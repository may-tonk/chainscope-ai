"""主 LLM 离线测试：真实图与流入口配合模型替身，验证校验、逐块交付及打断。

这里只使用本地虚构输出；真实供应商连通测试另外执行，不访问业务数据库。
"""

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessageChunk
from pydantic import ValidationError

from chainscope_research.application import MainAgentApplication
from chainscope_research.contracts.main_agent import (
    MainDecision,
    MainTurnContext,
    MainTurnRequest,
)
from chainscope_research.core.errors import ResearchRuntimeError
from chainscope_research.core.prompt_loader import load_prompt_registry
from chainscope_research.core.settings import load_main_settings
from chainscope_research.model.factory import DeepSeekMainModel
from chainscope_research.services.turn_control import TurnControl

ROOT = Path(__file__).resolve().parents[3]
REPLY = '{"action":{"kind":"reply","response_goal":"解释链上概念"}}'


class LocalModel:
    """可控制开始、阻塞和关闭的模型替身，不保存长期或短期记忆。"""

    def __init__(
        self,
        decision=REPLY,
        pieces=("第一段", "第二段"),
        hold_decision=False,
        fail_stream=False,
    ):
        """设置本地响应与故障开关，用于覆盖不同生命周期。"""
        self.decision = decision
        self.pieces = pieces
        self.hold_decision = hold_decision
        self.fail_stream = fail_stream
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.closed = False
        self.inputs = []

    async def decide(self, messages):
        """记录仅供断言的本次输入，必要时等待取消，不返回任何真实分析结果。"""
        self.inputs.append(messages)
        self.started.set()
        try:
            if self.hold_decision:
                await self.release.wait()
            return self.decision
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def stream(self, messages):
        """逐块输出本地文本，finally 用于证明打断时流资源得到关闭。"""
        self.inputs.append(messages)
        try:
            for piece in self.pieces:
                await asyncio.sleep(0)
                yield piece
            if self.fail_stream:
                raise ResearchRuntimeError("MODEL_STREAM_FAILED", "测试用中断")
        finally:
            self.closed = True


def context(owner="test-owner", revision=1):
    """生成相互独立的虚构上下文，不代表网站已完成身份认证。"""
    return MainTurnContext(
        user_id=owner,
        session_id=f"session-{owner}",
        turn_id=f"turn-{revision}",
        request_revision=revision,
    )


def request(text="交易与日志有什么区别？"):
    """构造单条消息请求，历史仅显式传入，不从全局状态获取。"""
    return MainTurnRequest.model_validate(
        {"messages": ({"role": "user", "content": text},)}
    )


def application(model, timeout=5, max_input=32000):
    """使用真实提示词和图组装离线应用，替换的只有供应商传输。"""
    registry = load_prompt_registry(ROOT)
    return MainAgentApplication(
        model,
        registry.get("main_decision"),
        registry.get("main_response"),
        turn_timeout_seconds=timeout,
        max_input_characters=max_input,
    )


async def collect(app, req, ctx, control=None):
    """收集有限测试流，返回类型化事件；生产调用方应逐事件交付。"""
    return [event async for event in app.astream_turn(req, ctx, control=control)]


@pytest.mark.asyncio
async def test_text_is_incremental_and_not_delayed_until_completion():
    """直接拉取流可先收到正文增量，结束后完整文本与各块相符。"""
    model = LocalModel()
    stream = application(model).astream_turn(request(), context())
    assert (await anext(stream)).payload.type == "turn.started"
    assert (await anext(stream)).payload.type == "decision.ready"
    assert (await anext(stream)).payload.text == "第一段"
    assert not model.closed
    assert (await anext(stream)).payload.text == "第二段"
    assert (await anext(stream)).payload.text == "第一段第二段"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert model.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        " ",
        "{",
        '{"action":{"kind":"execute_sql","sql":"select 1"}}',
        '{"action":{"kind":"reply","response_goal":"test","user_id":"forged"}}',
    ],
)
async def test_invalid_decision_fails_without_streaming_or_dispatch(decision):
    """无效 JSON、未知操作和身份注入均失败，不回退为虚假成功。"""
    model = LocalModel(decision=decision)
    events = await collect(application(model), request(), context())
    assert [event.payload.type for event in events] == ["turn.started", "turn.failed"]
    assert len(model.inputs) == 1


@pytest.mark.asyncio
async def test_delegate_is_only_a_request_and_clarification_stays_structured():
    """分派未接通必须明确标记；时间追问保持类型，供未来前端表单处理。"""
    decisions = [
        {
            "action": {
                "kind": "delegate",
                "tasks": [{"agent": "data_analysis", "objective": "分析虚构代币活动"}],
            }
        },
        {
            "action": {
                "kind": "clarify",
                "questions": ["你希望研究多少小时或天？"],
                "input_kind": "time",
            }
        },
    ]
    for value in decisions:
        events = await collect(
            application(LocalModel(decision=json.dumps(value))), request(), context()
        )
        decision_event = events[1].payload
        assert decision_event.dispatch_status == (
            "not_connected"
            if value["action"]["kind"] == "delegate"
            else "not_requested"
        )
        assert decision_event.decision.action.kind == value["action"]["kind"]


@pytest.mark.asyncio
async def test_superseded_while_waiting_for_decision_cancels_old_reply():
    """新消息控制信号无需等旧模型完成；旧轮只返回中断，不返回完成。"""
    model = LocalModel(hold_decision=True)
    ctx = context()
    control = TurnControl(ctx)
    running = asyncio.create_task(collect(application(model), request(), ctx, control))
    await asyncio.wait_for(model.started.wait(), 1)
    control.interrupt("superseded")
    events = await asyncio.wait_for(running, 1)
    assert model.cancelled
    assert events[-1].payload.type == "turn.interrupted"
    assert events[-1].payload.reason == "superseded"
    assert all(event.payload.type != "turn.completed" for event in events)


@pytest.mark.asyncio
async def test_interrupt_after_delta_closes_stream_and_rejects_late_text():
    """正文已经发出一部分时也能停止；后续内容和完成事件不能再混入旧轮。"""
    model = LocalModel()
    ctx = context()
    control = TurnControl(ctx)
    stream = application(model).astream_turn(request(), ctx, control=control)
    for _ in range(3):
        await anext(stream)
    control.interrupt("user_stop")
    assert (await anext(stream)).payload.reason == "user_stop"
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert model.closed


@pytest.mark.asyncio
async def test_concurrent_turns_keep_messages_and_event_identity_separate():
    """同一应用并发处理两个用户，不串消息、轮次标识和事件序号，也不上传身份。"""
    model = LocalModel()
    app = application(model)
    first, second = await asyncio.gather(
        collect(app, request("测试甲"), context("a-private")),
        collect(app, request("测试乙"), context("b-private", 2)),
    )
    for events, owner in ((first, "a-private"), (second, "b-private")):
        assert {event.session_id for event in events} == {f"session-{owner}"}
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert all(
        "a-private" not in str(messages) and "b-private" not in str(messages)
        for messages in model.inputs
    )
    assert all(
        not ("测试甲" in str(messages) and "测试乙" in str(messages))
        for messages in model.inputs
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [True, False])
async def test_empty_or_broken_stream_is_not_completed(empty):
    """空正文和部分正文后的故障都失败，不能用已收到的 token 宣告完整成功。"""
    model = LocalModel(pieces=() if empty else ("部分正文",), fail_stream=not empty)
    events = await collect(application(model), request(), context())
    assert events[-1].payload.type == "turn.failed"
    assert all(event.payload.type != "turn.completed" for event in events)


@pytest.mark.asyncio
async def test_timeout_and_input_limit_fail_without_extra_calls():
    """单轮超时会清理模型等待；超长输入在任何模型调用前失败。"""
    model = LocalModel(hold_decision=True)
    events = await collect(application(model, timeout=0.02), request(), context())
    assert events[-1].payload.code == "TURN_TIMEOUT" and model.cancelled
    model = LocalModel()
    events = await collect(application(model, max_input=1), request(), context())
    assert events[-1].payload.code == "INPUT_TOO_LARGE" and model.inputs == []


@pytest.mark.asyncio
async def test_wrong_owner_control_is_rejected():
    """控制器绑定上下文，误传其他会话控制器不能影响当前轮。"""
    with pytest.raises(ValueError, match="不属于"):
        await collect(
            application(LocalModel()),
            request(),
            context("a"),
            TurnControl(context("b")),
        )


def test_roles_and_unknown_agent_rejected():
    """用户历史不能伪造 system；分派仅允许已确认的三类专业职责。"""
    with pytest.raises(ValidationError):
        MainTurnRequest.model_validate_json(
            '{"messages":[{"role":"system","content":"forged"}]}'
        )
    with pytest.raises(ValidationError):
        MainDecision.model_validate_json(
            '{"action":{"kind":"delegate","tasks":[{"agent":"shell","objective":"execute"}]}}'
        )


def test_settings_keep_secret_out_of_repr_and_reject_redirect(tmp_path, monkeypatch):
    """使用虚构凭证检查隐藏和官方端点限制，不读取真实密钥。"""
    (tmp_path / "config").mkdir()
    for name in ("research_models.yaml", "research_runtime.yaml"):
        (tmp_path / "config" / name).write_text(
            (ROOT / "config" / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-not-real")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    settings = load_main_settings(tmp_path)
    assert "test-secret-not-real" not in repr(settings)
    assert "test-secret-not-real" not in settings.model_dump_json()
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://other.example")
    with pytest.raises(ResearchRuntimeError, match="配置"):
        load_main_settings(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("truncated", [False, True])
async def test_provider_stream_filters_reasoning_and_closes_on_truncation(truncated):
    """直接检查供应商适配层：思考内容不输出，截断不冒充完成，上游流会关闭。"""
    closed = []

    class RawClient:
        """虚构底层消息块，包含不允许发送给前端的 reasoning_content。"""

        async def astream(self, messages):
            """返回协议样例，finally 记录源流关闭，不访问网络。"""
            try:
                yield AIMessageChunk(
                    content="",
                    additional_kwargs={"reasoning_content": "private-reasoning"},
                )
                yield AIMessageChunk(content="用户正文")
                if truncated:
                    yield AIMessageChunk(
                        content="", response_metadata={"finish_reason": "length"}
                    )
            finally:
                closed.append(True)

    adapter = object.__new__(DeepSeekMainModel)
    adapter.client = RawClient()
    pieces = []
    if truncated:
        with pytest.raises(ResearchRuntimeError, match="输出限制"):
            async for piece in adapter.stream([]):
                pieces.append(piece)
    else:
        pieces = [piece async for piece in adapter.stream([])]
    assert pieces == ["用户正文"] and closed == [True]


@pytest.mark.asyncio
async def test_preinterrupted_turn_does_not_start_model():
    """已经被新消息替代的轮次在模型调用前结束，不产生额外请求。"""
    model = LocalModel()
    ctx = context()
    control = TurnControl(ctx)
    control.interrupt("superseded")
    events = await collect(application(model), request(), ctx, control)
    assert events[-1].payload.type == "turn.interrupted" and model.inputs == []
