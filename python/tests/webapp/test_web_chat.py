"""网页聊天隔离验收：真实 FastAPI/CSRF/研究应用，虚构身份和模型，不调用外部服务。"""

import asyncio
import json
import secrets
from contextlib import suppress
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chainscope_research.application import MainAgentApplication
from chainscope_research.contracts.main_agent import MainTurnRequest
from chainscope_research.core.prompt_loader import load_prompt_registry
from webapp.api.dependencies import current_session
from webapp.app import create_app
from webapp.core.app_config import AppConfig
from webapp.core.keys import AuthKeys
from webapp.core.settings import ROOT
from webapp.services.sessions import SessionInfo, csrf_hash, csrf_token_for

ORIGIN = "https://testserver"


class LocalModel:
    """确定性测试模型，用分片和主动失败覆盖事件流，不能作为业务返回。"""

    def __init__(self):
        """每个测试独立保存闭流标记和已收到历史。"""
        self.fail = False
        self.wait = False
        self.closed = False
        self.history = []
        self.action = {"kind": "reply", "response_goal": "解释概念"}

    async def decide(self, messages):
        """返回虚构结构化决策，同时验证调用方确实传递多轮上下文。"""
        self.history = messages
        return json.dumps({"action": self.action})

    async def stream(self, messages):
        """输出两段中文；可在首段后阻塞或失败，验证取消与部分结果边界。"""
        try:
            yield "链上交易"
            if self.wait:
                await asyncio.Event().wait()
            if self.fail:
                raise RuntimeError("private provider details")
            yield "可以包含多条日志。"
        finally:
            self.closed = True


@pytest.fixture
def chat_app():
    """只覆盖测试身份来源，来源检查与 CSRF 校验仍真实执行，不连接账户库。"""
    keys = AuthKeys(*(secrets.token_bytes(32) for _ in range(4)))
    sid, uid = uuid4(), uuid4()
    token = csrf_token_for(keys, sid)
    session = SessionInfo(sid, "user", uid, csrf_hash(keys, token))
    model = LocalModel()
    registry = load_prompt_registry(ROOT)
    main = MainAgentApplication(
        model,
        registry.get("main_decision"),
        registry.get("main_response"),
        turn_timeout_seconds=10,
        max_input_characters=32000,
    )
    app = create_app(
        engine=object(),
        keys=keys,
        config=AppConfig("development", (ORIGIN,), ("testserver",), "console"),
        mailer=object(),
        chat_application=main,
    )
    app.dependency_overrides[current_session] = lambda: session
    return app, model, {"Origin": ORIGIN, "X-CSRF-Token": token}, session


def stream_events(response):
    """解析实际 SSE 响应，不把注释心跳当成消息。"""
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def test_stream_and_multi_turn_history(chat_app):
    """接收真实研究应用的增量，最终文本不重复，历史用户/助手消息按角色传递。"""
    app, model, headers, _ = chat_app
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post(
            "/api/chat/turn",
            headers=headers,
            json={
                "messages": [
                    {"role": "user", "content": "前一个问题"},
                    {"role": "assistant", "content": "前一个回答"},
                    {"role": "user", "content": "继续解释"},
                ]
            },
        )
    assert response.status_code == 200, response.text
    assert "text/event-stream" in response.headers["content-type"]
    events = stream_events(response)
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    chunks = [
        event["payload"]["text"]
        for event in events
        if event["payload"]["type"] == "message.delta"
    ]
    assert len(chunks) == 2 and events[-1]["payload"]["text"] == "".join(chunks)
    # 决策只接收带角色的对话数据，不让历史自然语言变成 JSON 输出的错误示范。
    history = json.loads(model.history[-1].content)["messages"]
    assert history[1] == {"role": "assistant", "content": "前一个回答"}
    assert model.closed and app.state.chat._total == 0


@pytest.mark.parametrize(
    "case", ["csrf", "origin", "anonymous", "identity", "system", "last_assistant"]
)
def test_access_and_request_validation(chat_app, case):
    """真实 CSRF/来源检查拦截越权，模型不能收到身份字段或 system/tool 历史。"""
    app, _, headers, session = chat_app
    payload = {"messages": [{"role": "user", "content": "你好"}]}
    expected = 400
    if case == "csrf":
        headers = {"Origin": ORIGIN}
        expected = 403
    elif case == "origin":
        headers = {**headers, "Origin": "https://evil.example"}
        expected = 403
    elif case == "anonymous":
        app.dependency_overrides[current_session] = lambda: SessionInfo(
            session.session_id, "anonymous", None, session.csrf_hash
        )
        expected = 401
    elif case == "identity":
        payload["user_id"] = "other"
    elif case == "system":
        payload["messages"][0]["role"] = "system"
    else:
        payload["messages"][0]["role"] = "assistant"
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post("/api/chat/turn", headers=headers, json=payload)
    assert response.status_code == expected
    assert app.state.chat._total == 0


def test_partial_stream_failure_is_not_success(chat_app):
    """已有正文后发生故障，保留分片并输出失败终态，原始异常不出站。"""
    app, model, headers, _ = chat_app
    model.fail = True
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post(
            "/api/chat/turn",
            headers=headers,
            json={"messages": [{"role": "user", "content": "你好"}]},
        )
    events = stream_events(response)
    assert any(event["payload"]["type"] == "message.delta" for event in events)
    assert events[-1]["payload"]["type"] == "turn.failed"
    assert "private provider" not in response.text and app.state.chat._total == 0


def test_budget_and_pending_dispatch(chat_app):
    """超长输入和资源预算在开始流前拒绝；分派只报告未接通，不伪造分析。"""
    app, model, headers, session = chat_app
    with TestClient(app, base_url=ORIGIN) as client:
        large = client.post(
            "/api/chat/turn",
            headers=headers,
            json={"messages": [{"role": "user", "content": "x" * 32001}]},
        )
        assert large.status_code == 400 and app.state.chat._total == 0
        for _ in range(app.state.chat.settings.max_turns_per_user):
            assert app.state.chat.acquire(str(session.user_id))
        busy = client.post(
            "/api/chat/turn",
            headers=headers,
            json={"messages": [{"role": "user", "content": "你好"}]},
        )
        assert busy.status_code == 429
        for _ in range(app.state.chat.settings.max_turns_per_user):
            app.state.chat.release(str(session.user_id))
        model.action = {
            "kind": "delegate",
            "tasks": [{"agent": "data_analysis", "objective": "研究问题"}],
        }
        response = client.post(
            "/api/chat/turn",
            headers=headers,
            json={"messages": [{"role": "user", "content": "分析链上数据"}]},
        )
    decision = next(
        item["payload"]
        for item in stream_events(response)
        if item["payload"]["type"] == "decision.ready"
    )
    assert decision["dispatch_status"] == "not_connected"


@pytest.mark.asyncio
async def test_cancel_closes_upstream_and_returns_capacity(chat_app):
    """取消正在等待下一分片的任务，研究应用关闭源并归还真实资源。"""
    app, model, _, session = chat_app
    model.wait = True
    service = app.state.chat
    user = str(session.user_id)
    assert service.acquire(user)
    released = False

    def release():
        """测试租约与路由相同，保证退出时只归还一次。"""
        nonlocal released
        if not released:
            service.release(user)
            released = True

    async def authorized():
        """取消测试不涉及真实账户库，授权保持有效。"""
        return True

    source = service.events(
        await service.application(),
        MainTurnRequest.model_validate(
            {"messages": ({"role": "user", "content": "你好"},)}
        ),
        user,
        authorized,
        release,
    )
    while True:
        event = json.loads((await anext(source))["data"])
        if event["payload"]["type"] == "message.delta":
            break
    pending = asyncio.create_task(anext(source))
    await asyncio.sleep(0)
    pending.cancel()
    with suppress(asyncio.CancelledError):
        await pending
    await source.aclose()
    assert model.closed and service._total == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("during_stream", [False, True])
async def test_http_disconnect_releases_request(chat_app, during_stream):
    """模拟真实 ASGI 断连：开始前和首分片后都归还名额，正在等待的模型源会关闭。"""
    app, model, headers, _ = chat_app
    model.wait = True
    first_chunk = asyncio.Event()
    received_body = False

    async def receive():
        """先提交合法请求，再按场景发出浏览器断连事件；不用缓冲完整响应的测试客户端。"""
        nonlocal received_body
        if not received_body:
            received_body = True
            return {
                "type": "http.request",
                "body": json.dumps(
                    {"messages": [{"role": "user", "content": "你好"}]}
                ).encode(),
                "more_body": False,
            }
        if during_stream:
            await first_chunk.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        """只有观察到真实正文分片才触发中途断开，避免把取消前的空响应当成通过。"""
        if b"message.delta" in message.get("body", b""):
            first_chunk.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": "https",
        "method": "POST",
        "path": "/api/chat/turn",
        "raw_path": b"/api/chat/turn",
        "query_string": b"",
        "root_path": "",
        "server": ("testserver", 443),
        "client": ("127.0.0.1", 12345),
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            *((key.lower().encode(), value.encode()) for key, value in headers.items()),
        ],
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=3)
    assert app.state.chat._total == 0 and not app.state.chat._users
    if during_stream:
        assert first_chunk.is_set() and model.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_at", [1, 4])
async def test_revoked_session_ends_with_typed_failure(
    chat_app, monkeypatch, revoke_at
):
    """开始前及部分正文后撤销登录，都输出有序失败终态，不再发布后续正文。"""
    app, model, _, session = chat_app
    service = app.state.chat
    clock = iter(range(0, 1000, 20))
    # 只替换服务自己的时钟引用，不改变 asyncio 使用的系统时钟。
    monkeypatch.setattr(
        "webapp.services.chat.time", SimpleNamespace(monotonic=lambda: next(clock))
    )
    checks = 0

    async def authorize():
        """第 N 次发布检查时撤销会话，模拟长回复中的退出或账户失效。"""
        nonlocal checks
        checks += 1
        return checks < revoke_at

    user = str(session.user_id)
    assert service.acquire(user)
    events = [
        json.loads(item["data"])
        async for item in service.events(
            await service.application(),
            MainTurnRequest.model_validate_json(
                '{"messages":[{"role":"user","content":"你好"}]}'
            ),
            user,
            authorize,
            lambda: service.release(user),
        )
    ]
    assert events[-1]["payload"]["type"] == "turn.failed"
    assert events[-1]["payload"]["code"] == "not_authenticated"
    assert [item["sequence"] for item in events] == list(range(1, len(events) + 1))
    assert service._total == 0
    if revoke_at == 4:
        assert model.closed
        assert sum(item["payload"]["type"] == "message.delta" for item in events) == 1
