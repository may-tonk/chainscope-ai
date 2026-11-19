"""持久聊天接线验证：真实 PG、认证依赖与 SSE，模型使用明确本地替身。

所有账号来自随机测试夹具，不调用付费供应商；覆盖数据库先提交、刷新读取及断连收尾。
"""

import asyncio
import json
import secrets
from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from chainscope_research.application import MainAgentApplication
from chainscope_research.core.prompt_loader import load_prompt_registry
from chainscope_research.memory.contracts.conversations import SendMessageInput
from chainscope_research.memory.settings import load_memory_settings
from webapp.api.dependencies import current_session
from webapp.app import create_app
from webapp.core.app_config import AppConfig
from webapp.core.keys import AuthKeys
from webapp.core.settings import ROOT
from webapp.services.sessions import SessionInfo, csrf_hash, csrf_token_for

ORIGIN = "https://testserver"


class TestModel:
    """本地模型替身，可停在首段之后；不作为业务回答或供应商效果证据。"""

    __test__ = False

    def __init__(self):
        """记录调用及上下文，验证重放没有额外生成。"""
        self.calls = 0
        self.history = []
        self.wait = False
        self.closed = False

    async def decide(self, messages):
        """保存从服务端装配的历史，返回固定合法决策。"""
        self.calls += 1
        self.history = json.loads(messages[-1].content)["messages"]
        return '{"action":{"kind":"reply","response_goal":"本地接线验证"}}'

    async def stream(self, messages):
        """产生两个固定分片，或等待取消以验证流清理。"""
        try:
            yield "已落库第一段"
            if self.wait:
                await asyncio.Event().wait()
            yield "第二段"
        finally:
            self.closed = True


@pytest.fixture
def persistent_app(memory_store):
    """注入真实测试存储与虚构身份，来源检查及 CSRF 保持真实执行。"""
    store, user, other, migrator = memory_store
    keys = AuthKeys(*(secrets.token_bytes(32) for _ in range(4)))
    sid = uuid4()
    token = csrf_token_for(keys, sid)
    session = SessionInfo(sid, "user", user.user_id, csrf_hash(keys, token))
    model = TestModel()
    registry = load_prompt_registry(ROOT)
    main = MainAgentApplication(
        model,
        registry.get("main_decision"),
        registry.get("main_response"),
        turn_timeout_seconds=10,
        max_input_characters=32000,
    )
    app = create_app(
        engine=store.engine,
        keys=keys,
        config=AppConfig("development", (ORIGIN,), ("testserver",), "console"),
        mailer=object(),
        chat_application=main,
        memory_store=store,
    )
    app.dependency_overrides[current_session] = lambda: session
    return app, model, {"Origin": ORIGIN, "X-CSRF-Token": token}, user, other, migrator


def events(response):
    """解析实际 SSE 帧，忽略心跳注释。"""
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def test_persistent_http_stream_history_and_replay(persistent_app):
    """创建、生成、全新 GET 恢复、追问与请求重放构成持久闭环。"""
    app, model, headers, _, _, _ = persistent_app
    with TestClient(app, base_url=ORIGIN) as client:
        row = client.post(
            "/api/memory/conversations", headers=headers, json={"title": "持久样例"}
        ).json()["conversation"]
        cid = row["conversation_id"]
        body = {
            "conversation_id": cid,
            "client_request_id": str(uuid4()),
            "expected_revision": row["revision"],
            "content": "第一问",
        }
        response = client.post(
            f"/api/memory/conversations/{cid}/turns", headers=headers, json=body
        )
        assert response.status_code == 200, response.text
        frames = events(response)
        assert frames[-1]["payload"]["type"] == "turn.completed"
        assert [f["sequence"] for f in frames] == list(range(1, len(frames) + 1))
        snapshot = client.get(f"/api/memory/conversations/{cid}").json()
        assert snapshot["messages"][1]["content"] == "已落库第一段第二段"
        assert snapshot["messages"][1]["status"] == "completed"
        assert (
            snapshot["poll_after_ms"]
            == load_memory_settings().history.poll_seconds * 1000
        )
        replay = client.post(
            f"/api/memory/conversations/{cid}/turns", headers=headers, json=body
        )
        assert (
            events(replay)[-1]["payload"]["type"] == "turn.completed"
            and model.calls == 1
        )
        body = {
            **body,
            "client_request_id": str(uuid4()),
            "expected_revision": snapshot["conversation"]["revision"],
            "content": "继续追问",
        }
        assert (
            client.post(
                f"/api/memory/conversations/{cid}/turns", headers=headers, json=body
            ).status_code
            == 200
        )
        assert [item["role"] for item in model.history] == ["user", "assistant", "user"]
        assert (
            client.get("/api/memory/conversations").json()["conversations"][0][
                "conversation_id"
            ]
            == cid
        )
        assert app.state.chat._total == 0


def test_persistent_api_user_isolation_and_identity_injection(persistent_app):
    """真实 API 拒绝越权会话和模型历史/身份字段注入。"""
    app, _, headers, _, other, _ = persistent_app
    foreign = app.state.memory_chat.store.create_conversation(other, "他人的会话")
    with TestClient(app, base_url=ORIGIN) as client:
        assert (
            client.get(
                f"/api/memory/conversations/{foreign.conversation_id}"
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/memory/conversations",
                headers=headers,
                json={"user_id": str(other.user_id)},
            ).status_code
            == 400
        )
        assert client.post("/api/memory/conversations", json={}).status_code == 403
        assert client.get("/api/memory/conversations?limit=99999").status_code == 400


def test_disconnect_saves_partial_and_releases_slot(persistent_app):
    """在真实异步生成器中关闭连接；已交付分片已落库，轮次收尾且取消模型。"""
    app, model, _, user, _, _ = persistent_app
    model.wait = True
    service = app.state.memory_chat
    row = service.store.create_conversation(user, "断连样例")
    request = SendMessageInput(
        conversation_id=row.conversation_id,
        client_request_id=uuid4(),
        expected_revision=row.revision,
        content="测试中断",
    )

    async def run():
        """仅模拟客户端关闭读取，数据库和流应用仍为实际实现。"""
        main = await service.chat.application()
        accepted, prompt = await service.prepare(user, request, main)
        assert service.chat.acquire(str(user.user_id))

        async def authorize():
            """测试身份固定有效，取消由客户端主动产生。"""
            return True

        stream = service.events(
            main,
            prompt,
            user,
            accepted,
            authorize,
            lambda: service.chat.release(str(user.user_id)),
        )
        while True:
            frame = json.loads((await anext(stream))["data"])
            if frame["payload"]["type"] == "message.delta":
                saved = await service.db(
                    service.store.list_messages, user, row.conversation_id
                )
                assert saved[1].content == frame["payload"]["text"]
                break
        await stream.aclose()
        return accepted.turn.turn_id

    turn_id = asyncio.run(run())
    saved = service.store.get_turn(user, row.conversation_id, turn_id)
    assert saved.status == "stopped" and model.closed and app.state.chat._total == 0


def test_expired_worker_is_recovered_by_history_read(persistent_app):
    """进程中断留下的旧轮次按配置水位恢复，保留正文，不自动续跑模型。"""
    app, model, _, user, _, migrator = persistent_app
    service = app.state.memory_chat
    row = service.store.create_conversation(user, "过期样例")
    accepted = service.store.accept_message(
        user,
        SendMessageInput(
            conversation_id=row.conversation_id,
            client_request_id=uuid4(),
            expected_revision=1,
            content="此前的问题",
        ),
    )
    # 构造历史时间样例，所有时间一起回移，仍遵守数据库时间约束。
    seconds = service.settings.recovery.stale_after_seconds + 1
    with migrator.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE research.conversation_turns SET created_at=created_at-:age,updated_at=updated_at-:age WHERE turn_id=:id"
            ),
            {"age": timedelta(seconds=seconds), "id": accepted.turn.turn_id},
        )
    snapshot = asyncio.run(service.snapshot(user, row.conversation_id))
    assert snapshot["messages"][1]["status"] == "stopped" and model.calls == 0


def test_memory_config_rejects_duplicate_unknown_and_invalid_ranges(tmp_path):
    """配置错误在装配时拒绝，错误提示不回显 YAML 正文。"""
    path = tmp_path / "config/memory"
    path.mkdir(parents=True)
    for content in ("secret: do-not-print", "history: {}\nhistory: {}", "{}"):
        (path / "runtime.yaml").write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match="Memory 运行设置") as error:
            load_memory_settings(tmp_path)
        assert "do-not-print" not in str(error.value)


def test_yaml_flush_watermark_changes_actual_sse_batches(persistent_app, tmp_path):
    """从另一份实际 YAML 读取批次水位，验证配置真正影响持久 SSE，而非只声明字段。"""
    app, _, headers, _, _, _ = persistent_app
    folder = tmp_path / "config/memory"
    folder.mkdir(parents=True)
    text = (ROOT / "config/memory/runtime.yaml").read_text(encoding="utf-8")
    assert "flush_characters: 512" in text
    (folder / "runtime.yaml").write_text(
        text.replace("flush_characters: 512", "flush_characters: 1"), encoding="utf-8"
    )
    app.state.memory_chat.settings = load_memory_settings(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        row = client.post("/api/memory/conversations", headers=headers, json={}).json()[
            "conversation"
        ]
        response = client.post(
            f"/api/memory/conversations/{row['conversation_id']}/turns",
            headers=headers,
            json={
                "conversation_id": row["conversation_id"],
                "client_request_id": str(uuid4()),
                "expected_revision": row["revision"],
                "content": "检查实际水位",
            },
        )
        deltas = [
            item
            for item in events(response)
            if item["payload"]["type"] == "message.delta"
        ]
        assert len(deltas) == 2
