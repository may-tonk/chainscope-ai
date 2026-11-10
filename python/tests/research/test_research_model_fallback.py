"""主备故障与配置接线测试；使用本地模型替身和 HTTP 异常，不制造线上故障。"""

import asyncio
from pathlib import Path

import httpx
import pytest
import yaml
from openai import APIConnectionError, APIStatusError
from pydantic import SecretStr, ValidationError

from chainscope_research import bootstrap
from chainscope_research.application import MainAgentApplication
from chainscope_research.contracts.main_agent import MainTurnContext, MainTurnRequest
from chainscope_research.core.config_schema import ModelConfiguration, RuntimeSettings
from chainscope_research.core.errors import ResearchRuntimeError
from chainscope_research.core.prompt_loader import load_prompt_registry
from chainscope_research.core.settings import load_main_settings
from chainscope_research.model.factory import (
    DeepSeekMainModel,
    ModelCallError,
    classify_provider_error,
)
from chainscope_research.model.fallback import ResilientModels
from chainscope_research.services.turn_control import TurnControl

ROOT = Path(__file__).resolve().parents[3]
VALID = '{"action":{"kind":"reply","response_goal":"解释概念"}}'


class ScriptedModel:
    """可声明决策、流前故障和流后故障的无网络替身。"""

    def __init__(self, decision=VALID, before=None, after=None):
        """初始化故障脚本与调用次数，仅记录测试证据。"""
        self.decision = decision
        self.before = before
        self.after = after
        self.calls = []
        self.waiting = asyncio.Event()

    async def decide(self, messages):
        """返回结构化文本或抛出受控决策故障。"""
        self.calls.append("decision")
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision

    async def stream(self, messages):
        """返回有无正文两个故障边界，也可等待取消。"""
        self.calls.append("response")
        if self.before:
            raise self.before
        yield "测试正文"
        if self.after:
            raise self.after


def runtime(**changes):
    """生成有效运行配置，测试按需收紧预算。"""
    return RuntimeSettings(
        turn_timeout_seconds=5,
        max_input_characters=32000,
        max_output_characters=64000,
        max_attempts_per_stage=2,
        max_attempts_per_turn=4,
        **changes,
    )


def pool(primary, backup, policy=None):
    """组装实际切换层，替换的仅为模型网络传输。"""
    return ResilientModels(
        (("primary", primary), ("backup", backup)), policy or runtime()
    )


def test_turn_timeout_accepts_1000_and_rejects_values_outside_range():
    """用户确认整轮最多 1000 秒；不改变单次请求或阶段次数预算。"""
    values = runtime().model_dump()
    assert (
        RuntimeSettings.model_validate(
            {**values, "turn_timeout_seconds": 1000}
        ).turn_timeout_seconds
        == 1000
    )
    for invalid in (0, -1, 1001):
        with pytest.raises(ValidationError):
            RuntimeSettings.model_validate({**values, "turn_timeout_seconds": invalid})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["not-json", ModelCallError("MODEL_UNAVAILABLE", "测试故障", True)]
)
async def test_recoverable_or_bad_schema_uses_backup_once(bad):
    """连接故障或坏结构可以切换，且备用必须通过相同 Pydantic 校验。"""
    primary, backup = ScriptedModel(bad), ScriptedModel()
    turn = pool(primary, backup).for_turn()
    assert await turn.decide([]) == VALID
    assert [record.outcome for record in turn.records] == ["failed", "success"]
    assert primary.calls == backup.calls == ["decision"]


@pytest.mark.asyncio
async def test_stream_before_text_switches_but_after_text_does_not():
    """流前恢复，流后中断；绝不把主、备两段正文拼接。"""
    failure = ModelCallError("MODEL_UNAVAILABLE", "测试故障", True)
    backup = ScriptedModel()
    turn = pool(ScriptedModel(before=failure), backup).for_turn()
    assert [piece async for piece in turn.stream([])] == ["测试正文"]
    assert len(turn.records) == 2 and backup.calls == ["response"]
    backup = ScriptedModel()
    turn = pool(ScriptedModel(after=failure), backup).for_turn()
    pieces = []
    with pytest.raises(ModelCallError):
        async for piece in turn.stream([]):
            pieces.append(piece)
    assert pieces == ["测试正文"] and backup.calls == []


@pytest.mark.asyncio
async def test_nonrecoverable_and_shared_budget_prevent_more_calls():
    """权限失败不换模型；决策耗尽预算后正文不能另起一套计数。"""
    backup = ScriptedModel()
    turn = pool(
        ScriptedModel(ModelCallError("MODEL_AUTH_FAILED", "测试拒绝", False)), backup
    ).for_turn()
    with pytest.raises(ModelCallError):
        await turn.decide([])
    assert backup.calls == []
    policy = runtime().model_copy(update={"max_attempts_per_turn": 2})
    turn = pool(ScriptedModel("broken"), ScriptedModel(), policy).for_turn()
    await turn.decide([])
    with pytest.raises(ResearchRuntimeError, match="上限"):
        _ = [piece async for piece in turn.stream([])]
    assert len(turn.records) == 2


@pytest.mark.asyncio
async def test_all_failures_stop_and_two_turns_have_independent_budgets():
    """主备均坏时明确失败，不递归；共享客户端的两轮具有独立记录。"""
    resource = pool(ScriptedModel("broken"), ScriptedModel("also-broken"))
    first, second = resource.for_turn(), resource.for_turn()
    for turn in (first, second):
        with pytest.raises(ResearchRuntimeError, match="均未"):
            await turn.decide([])
        assert len(turn.records) == 2
    assert first.records is not second.records


@pytest.mark.parametrize("status", [400, 401, 402, 403, 422, 429, 500, 502, 503, 504])
def test_http_errors_classified_without_leaking_body(status):
    """按状态筛选恢复，错误正文中的凭证样例不会进入公开消息。"""
    request = httpx.Request("POST", "https://api.deepseek.com")
    error = APIStatusError(
        "private-secret",
        response=httpx.Response(status, request=request),
        body={"private": "secret"},
    )
    classified = classify_provider_error(error)
    assert classified.recoverable == (status in {500, 502, 503, 504})
    assert "private-secret" not in str(classified)
    assert classify_provider_error(APIConnectionError(request=request)).recoverable


def fixture_config(tmp_path, monkeypatch):
    """复制非秘密 YAML 测试样例并设置虚构凭证，不修改真实环境或真实 .env。"""
    directory = tmp_path / "config"
    directory.mkdir()
    for name in ("research_models.yaml", "research_runtime.yaml"):
        (directory / name).write_text(
            (ROOT / "config" / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    for name in (
        "DEEPSEEK_MODEL",
        "CHAINSCOPE_RESEARCH_MODEL",
        "CHAINSCOPE_RESEARCH_FALLBACK_MODEL",
        "DEEPSEEK_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
    return load_main_settings(tmp_path)


def test_yaml_parameters_reach_actual_clients(tmp_path, monkeypatch):
    """检查设置真正传到 SDK，而不是 YAML 中声明了却没有使用。"""
    settings = fixture_config(tmp_path, monkeypatch)
    model = DeepSeekMainModel(settings.models.primary, settings.primary_key)
    assert (
        model.decision_client.temperature
        == settings.models.primary.decision.temperature
    )
    assert model.client.temperature == settings.models.primary.response.temperature
    assert model.client.max_tokens == settings.models.primary.response.max_tokens
    assert (
        model.client.request_timeout == settings.models.primary.request_timeout_seconds
    )
    assert model.client.max_retries == 0
    assert model.client.extra_body == {
        "thinking": {"type": settings.models.primary.response.thinking}
    }
    assert "test-only-secret" not in repr(settings) + settings.model_dump_json()
    profile = settings.models.primary.model_copy(
        update={
            "response": settings.models.primary.response.model_copy(
                update={"temperature": 0.6, "max_tokens": 512}
            )
        }
    )
    changed = DeepSeekMainModel(profile, SecretStr("fake"))
    assert changed.client.temperature == 0.6 and changed.client.max_tokens == 512


def test_yaml_values_reach_application_and_both_model_stages(tmp_path, monkeypatch):
    """只修改临时 YAML，确认整轮限制、主备两阶段参数均由装配入口原样传入。

    校验边界属于代码合同；实际使用值只有 YAML/允许的环境覆盖这一处来源。
    不调用供应商，也不修改真实配置，避免用户调参被测试回写覆盖。
    """
    fixture_config(tmp_path, monkeypatch)
    model_path = tmp_path / "config/research_models.yaml"
    model_values = yaml.safe_load(model_path.read_text(encoding="utf-8"))
    for role, temperature, timeout in (("primary", 0.6, 33), ("fallback", 0.7, 34)):
        model_values[role]["request_timeout_seconds"] = timeout
        model_values[role]["decision"]["temperature"] = temperature
        model_values[role]["decision"]["max_tokens"] = 512
        model_values[role]["response"]["temperature"] = temperature + 0.1
        model_values[role]["response"]["max_tokens"] = 768
    model_path.write_text(yaml.safe_dump(model_values), encoding="utf-8")
    runtime_path = tmp_path / "config/research_runtime.yaml"
    runtime_values = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    runtime_values.update(
        turn_timeout_seconds=1000,
        max_input_characters=33000,
        max_output_characters=65000,
    )
    runtime_path.write_text(yaml.safe_dump(runtime_values), encoding="utf-8")
    registry = load_prompt_registry(ROOT)

    def existing_prompts(project_root):
        """复用已验证模板，当前测试专门检查模型与运行设置的真实接线。"""
        return registry

    monkeypatch.setattr(bootstrap, "load_prompt_registry", existing_prompts)
    app = bootstrap.build_main_application(tmp_path)
    assert app.turn_timeout_seconds == runtime_values["turn_timeout_seconds"]
    assert app.max_input_characters == runtime_values["max_input_characters"]
    assert app.max_output_characters == runtime_values["max_output_characters"]
    assert (
        app.model.runtime.max_attempts_per_turn
        == runtime_values["max_attempts_per_turn"]
    )
    clients = dict(app.model.models)
    for role in ("primary", "fallback"):
        profile = model_values[role]
        adapter = clients[profile["model"]]
        for stage, client in (
            ("decision", adapter.decision_client),
            ("response", adapter.client),
        ):
            assert client.temperature == profile[stage]["temperature"]
            assert client.max_tokens == profile[stage]["max_tokens"]
            assert client.request_timeout == profile["request_timeout_seconds"]


@pytest.mark.parametrize(
    "kind",
    ["duplicate", "unknown", "invalid", "unsafe", "same_model", "ignored_sampling"],
)
def test_bad_yaml_and_combinations_fail_early(tmp_path, monkeypatch, kind):
    """重复键、拼错字段、越界、对象标签和无效模型组合均在网络调用前拒绝。"""
    settings = fixture_config(tmp_path, monkeypatch)
    file = tmp_path / "config/research_runtime.yaml"
    if kind == "duplicate":
        file.write_text(
            file.read_text(encoding="utf-8") + "\nturn_timeout_seconds: 60\n",
            encoding="utf-8",
        )
    elif kind == "unknown":
        file.write_text(
            file.read_text(encoding="utf-8") + "\ntempature: 1\n", encoding="utf-8"
        )
    elif kind == "invalid":
        # 按字段构造越界值，不依赖实际配置仍然是旧的 120 秒。
        invalid = yaml.safe_load(file.read_text(encoding="utf-8"))
        invalid["turn_timeout_seconds"] = -1
        file.write_text(yaml.safe_dump(invalid), encoding="utf-8")
    elif kind == "unsafe":
        file.write_text("!!python/object:builtins.object {}", encoding="utf-8")
    else:
        payload = settings.models.model_dump()
        if kind == "same_model":
            payload["fallback"]["model"] = payload["primary"]["model"]
        else:
            payload["primary"]["response"]["temperature"] = None
            payload["primary"]["response"]["top_p"] = 0.5
        with pytest.raises(ValidationError):
            ModelConfiguration.model_validate(payload)
        return
    with pytest.raises(ResearchRuntimeError):
        load_main_settings(tmp_path)


@pytest.mark.asyncio
async def test_application_retains_audit_and_cancellation_never_falls_back():
    """在真实应用图内验证尝试审计，以及预先停止后零模型调用。"""
    primary, backup = ScriptedModel("broken"), ScriptedModel()
    registry = load_prompt_registry(ROOT)
    app = MainAgentApplication(
        pool(primary, backup),
        registry.get("main_decision"),
        registry.get("main_response"),
        turn_timeout_seconds=5,
        max_input_characters=32000,
    )
    ctx = MainTurnContext(
        user_id="test", session_id="test", turn_id="test", request_revision=1
    )
    req = MainTurnRequest.model_validate(
        {"messages": ({"role": "user", "content": "测试"},)}
    )
    events = [event async for event in app.astream_turn(req, ctx)]
    assert events[-1].payload.type == "turn.completed"
    assert [item.model for item in events[-1].model_attempts] == [
        "primary",
        "backup",
        "primary",
    ]
    control = TurnControl(ctx)
    control.interrupt("superseded")
    primary.calls.clear()
    backup.calls.clear()
    events = [event async for event in app.astream_turn(req, ctx, control=control)]
    assert events[-1].payload.type == "turn.interrupted"
    assert primary.calls == backup.calls == []


@pytest.mark.asyncio
async def test_cancel_inflight_primary_never_uses_backup():
    """主模型等待中被新消息取代时取消等待，禁止进入备用分支。"""

    class WaitingModel(ScriptedModel):
        """仅用于等待取消的无网络替身。"""

        async def decide(self, messages):
            """通知测试已进入调用，再等待外部取消，不产生虚构成功。"""
            self.calls.append("decision")
            self.waiting.set()
            await asyncio.Future()
            return VALID

    primary, backup = WaitingModel(), ScriptedModel()
    resource = pool(primary, backup)
    turn = resource.for_turn()
    running = asyncio.create_task(turn.decide([]))
    await asyncio.wait_for(primary.waiting.wait(), 1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert backup.calls == [] and turn.records[0].outcome == "interrupted"


@pytest.mark.asyncio
async def test_stage_limit_disabled_fallback_and_empty_output():
    """单次阶段策略不尝试备用；无正文可切换，只有主模型时明确结束。"""
    backup = ScriptedModel()
    policy = runtime().model_copy(update={"max_attempts_per_stage": 1})
    turn = pool(ScriptedModel("broken"), backup, policy).for_turn()
    with pytest.raises(ResearchRuntimeError):
        await turn.decide([])
    assert backup.calls == []

    class EmptyModel(ScriptedModel):
        """输出空片段模拟供应商没有正文。"""

        async def stream(self, messages):
            """只输出空片段，切换层不得把它视为已交付正文。"""
            yield ""
            yield " \n"

    turn = pool(EmptyModel(), backup).for_turn()
    assert [piece async for piece in turn.stream([])] == ["测试正文"]
    only_primary = ResilientModels((("primary", EmptyModel()),), runtime()).for_turn()
    with pytest.raises(ResearchRuntimeError):
        _ = [piece async for piece in only_primary.stream([])]
