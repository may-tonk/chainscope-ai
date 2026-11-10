"""提示词登记、模板边界和主备接线测试；全部使用临时文件与本地模型替身。"""

import asyncio
import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from chainscope_research import bootstrap
from chainscope_research.application import MainAgentApplication
from chainscope_research.contracts.main_agent import MainTurnContext, MainTurnRequest
from chainscope_research.core.config_schema import RuntimeSettings
from chainscope_research.core.errors import ResearchRuntimeError
from chainscope_research.core.prompt_loader import load_prompt_registry
from chainscope_research.model.factory import ModelCallError
from chainscope_research.model.fallback import ResilientModels

ROOT = Path(__file__).resolve().parents[3]


def temporary_project(tmp_path):
    """创建无凭证的临时登记表，路径故意区别于生产目录，验证 YAML 路由生效。"""
    (tmp_path / "config").mkdir()
    directory = tmp_path / "prompts/custom"
    directory.mkdir(parents=True)
    (directory / "decision.txt").write_text(
        '决策标记 {{decision_schema}} 示例 {"key": "value"}', encoding="utf-8"
    )
    (directory / "response.txt").write_text("回复标记 {{decision}}", encoding="utf-8")
    config = {
        "prompts": {
            "main_decision": {
                "file": "prompts/custom/decision.txt",
                "version": "test-decision",
                "required_variables": ["decision_schema"],
            },
            "main_response": {
                "file": "prompts/custom/response.txt",
                "version": "test-response",
                "required_variables": ["decision"],
            },
        }
    }
    (tmp_path / "config/research_prompts.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )
    return config


def save_config(tmp_path, config):
    """仅更新临时 YAML，不接触真实项目配置和提示词。"""
    (tmp_path / "config/research_prompts.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )


def test_paths_versions_json_braces_and_single_pass_render(tmp_path):
    """配置路径与版本能生效；JSON 花括号保留，变量里的模板记号不会二次展开。"""
    temporary_project(tmp_path)
    registry = load_prompt_registry(tmp_path)
    prompt = registry.get("main_decision")
    prompt.require_variables(("decision_schema",))
    data = '{"example":"{{other}}"}'
    result = prompt.render(decision_schema=data)
    assert data in result and '{"key": "value"}' in result
    assert prompt.reference.version == "test-decision"
    assert (
        prompt.reference.content_sha256
        == hashlib.sha256(prompt.content.encode("utf-8")).hexdigest()
    )
    assert data not in prompt.content
    assert "决策标记" not in repr(registry)


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"decision_schema": "x", "extra": "private-secret"},
        {"wrong": "private-secret"},
        {"decision_schema": " "},
        {"decision_schema": 123},
    ],
)
def test_bad_runtime_variables_do_not_echo_values(tmp_path, values):
    """缺参、多参、错误名字、空值和类型错误都明确失败，不回显敏感参数值。"""
    temporary_project(tmp_path)
    prompt = load_prompt_registry(tmp_path).get("main_decision")
    with pytest.raises(ResearchRuntimeError) as caught:
        prompt.render(**values)
    assert "private-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "case",
    [
        "missing_yaml",
        "empty_yaml",
        "duplicate_yaml",
        "unknown_field",
        "duplicate_variable",
        "undeclared_variable",
        "unused_variable",
        "malformed_variable",
        "empty_text",
        "missing_text",
        "outside_path",
        "absolute_path",
        "wrong_suffix",
        "bad_name",
    ],
)
def test_invalid_registration_fails_before_model_calls(tmp_path, case):
    """登记错误、资源缺失和越界在装配阶段失败，不能悄悄换成默认提示词。"""
    config = temporary_project(tmp_path)
    declaration = config["prompts"]["main_decision"]
    file = tmp_path / "prompts/custom/decision.txt"
    if case == "duplicate_variable":
        declaration["required_variables"] = ["decision_schema", "decision_schema"]
    elif case == "unused_variable":
        declaration["required_variables"] = ["decision_schema", "unused"]
    elif case == "unknown_field":
        declaration["unknown"] = "invalid"
    elif case == "bad_name":
        config["prompts"]["../bad"] = declaration
    elif case == "outside_path":
        (tmp_path / "private.txt").write_text("private-content", encoding="utf-8")
        declaration["file"] = "prompts/../private.txt"
    elif case == "absolute_path":
        declaration["file"] = str(file)
    elif case == "wrong_suffix":
        target = tmp_path / "prompts/custom/wrong.yaml"
        target.write_text(file.read_text(encoding="utf-8"), encoding="utf-8")
        declaration["file"] = "prompts/custom/wrong.yaml"
    elif case == "undeclared_variable":
        file.write_text(
            file.read_text(encoding="utf-8") + " {{extra}}", encoding="utf-8"
        )
    elif case == "malformed_variable":
        file.write_text("{{ decision_schema }}", encoding="utf-8")
    elif case == "empty_text":
        file.write_text(" \n", encoding="utf-8")
    elif case == "missing_text":
        file.unlink()
    save_config(tmp_path, config)
    yaml_path = tmp_path / "config/research_prompts.yaml"
    if case == "missing_yaml":
        yaml_path.unlink()
    elif case == "empty_yaml":
        yaml_path.write_text("", encoding="utf-8")
    elif case == "duplicate_yaml":
        yaml_path.write_text(
            yaml_path.read_text(encoding="utf-8") + "\nprompts: {}\n", encoding="utf-8"
        )
    with pytest.raises(ResearchRuntimeError) as caught:
        load_prompt_registry(tmp_path)
    assert "private-content" not in str(caught.value)


def test_registry_is_frozen_unknown_names_fail_and_reload_is_explicit(tmp_path):
    """模板对象和目录不可变；改文件不影响已装配对象，新加载才能看到新内容。"""
    temporary_project(tmp_path)
    registry = load_prompt_registry(tmp_path)
    prompt = registry.get("main_response")
    with pytest.raises(FrozenInstanceError):
        prompt.content = "changed"
    with pytest.raises(TypeError):
        registry.entries["main_response"] = prompt
    with pytest.raises(ResearchRuntimeError):
        registry.get("not_registered")
    with pytest.raises(ResearchRuntimeError):
        prompt.require_variables(("wrong",))
    (tmp_path / "prompts/custom/response.txt").write_text(
        "新正文 {{decision}}", encoding="utf-8"
    )
    reloaded = load_prompt_registry(tmp_path).get("main_response")
    assert prompt.render(decision="a") == "回复标记 a"
    assert reloaded.render(decision="a") == "新正文 a"
    assert reloaded.reference.content_sha256 != prompt.reference.content_sha256


def test_static_template_needs_no_variables(tmp_path):
    """后续纯角色说明可以没有变量，仍遵守登记、版本和路径规则。"""
    config = temporary_project(tmp_path)
    config["prompts"]["static"] = {
        "file": "prompts/custom/static.txt",
        "version": "1",
        "required_variables": [],
    }
    (tmp_path / "prompts/custom/static.txt").write_text("静态说明", encoding="utf-8")
    save_config(tmp_path, config)
    assert load_prompt_registry(tmp_path).get("static").render() == "静态说明"


def test_link_escape_is_rejected(tmp_path):
    """解析链接后仍应检查边界；Windows 无创建链接权限时显式跳过该用例。"""
    config = temporary_project(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "prompts/custom/link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前 Windows 环境不允许创建符号链接")
    config["prompts"]["main_decision"]["file"] = "prompts/custom/link.txt"
    save_config(tmp_path, config)
    with pytest.raises(ResearchRuntimeError):
        load_prompt_registry(tmp_path)


class RecordingModel:
    """捕获传入消息的本地替身，用于比较主备是否拿到相同渲染结果。"""

    def __init__(self, fail=False):
        """配置是否故障并保存测试消息，不访问任何实际模型或数据库。"""
        self.fail = fail
        self.decisions = []
        self.responses = []

    async def decide(self, messages):
        """记录决策输入，返回固定合法合同或可恢复故障。"""
        self.decisions.append(messages)
        if self.fail:
            raise ModelCallError("MODEL_UNAVAILABLE", "本地测试故障", True)
        return '{"action":{"kind":"reply","response_goal":"test"}}'

    async def stream(self, messages):
        """记录正文输入，逐块返回本地文本；主模型可在正文前故障。"""
        self.responses.append(messages)
        if self.fail:
            raise ModelCallError("MODEL_UNAVAILABLE", "本地测试故障", True)
        yield "本地"
        await asyncio.sleep(0)
        yield "样例"


@pytest.mark.asyncio
async def test_configured_prompts_reach_both_models_with_same_snapshot(tmp_path):
    """真实主 Agent 图与切换层使用 YAML 自定义路径，主备复用同一渲染文本。"""
    temporary_project(tmp_path)
    registry = load_prompt_registry(tmp_path)
    primary, backup = RecordingModel(True), RecordingModel()
    runtime = RuntimeSettings(
        turn_timeout_seconds=5,
        max_input_characters=32000,
        max_output_characters=64000,
        max_attempts_per_stage=2,
        max_attempts_per_turn=4,
    )
    app = MainAgentApplication(
        ResilientModels((("primary", primary), ("backup", backup)), runtime),
        registry.get("main_decision"),
        registry.get("main_response"),
        turn_timeout_seconds=5,
        max_input_characters=32000,
    )
    # 用户文本中的模板标记仍应原样位于 human 消息，不进入系统模板替换。
    req = MainTurnRequest.model_validate(
        {"messages": ({"role": "user", "content": "{{decision_schema}} 测试问题"},)}
    )
    ctx = MainTurnContext(
        user_id="private-user", session_id="test", turn_id="test", request_revision=1
    )
    events = [event async for event in app.astream_turn(req, ctx)]
    assert events[-1].payload.text == "本地样例"
    assert primary.decisions == backup.decisions
    assert primary.responses == backup.responses
    assert primary.decisions[0][0].content.startswith("决策标记 ")
    schema_text = (
        primary.decisions[0][0].content.split(" 示例 ")[0].removeprefix("决策标记 ")
    )
    assert "action" in json.loads(schema_text)["properties"]
    assert primary.decisions[0][1].type == "human"
    assert primary.decisions[0][1].content == req.messages[0].content
    assert primary.responses[0][0].content.startswith("回复标记 ")
    assert {ref.version for ref in events[-1].prompt_refs} == {
        "test-decision",
        "test-response",
    }
    assert "private-user" not in str(primary.decisions)


@pytest.mark.asyncio
async def test_rendered_user_values_are_not_cached_between_concurrent_turns(tmp_path):
    """并发使用同一已加载模板，每次渲染独立，不把参数值写回公共模板。"""
    temporary_project(tmp_path)
    prompt = load_prompt_registry(tmp_path).get("main_response")

    async def render(value):
        """让两个协程共享模板并交错执行，返回本次字符串结果。"""
        await asyncio.sleep(0)
        return prompt.render(decision=value)

    first, second = await asyncio.gather(render("甲"), render("乙"))
    assert first == "回复标记 甲" and second == "回复标记 乙"
    assert prompt.content == "回复标记 {{decision}}"


@pytest.mark.asyncio
async def test_bootstrap_loads_yaml_paths_without_model_network(tmp_path, monkeypatch):
    """从真实装配入口验证自定义路径，模型客户端替身避免依赖实际密钥和网络。"""
    temporary_project(tmp_path)
    for name in ("research_models.yaml", "research_runtime.yaml"):
        (tmp_path / "config" / name).write_text(
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
    clients = []

    def client_factory(profile, key):
        """接收已验证的模型配置，只构造本地记录器，不发送凭证到任何服务。"""
        model = RecordingModel()
        clients.append(model)
        return model

    monkeypatch.setattr(bootstrap, "DeepSeekMainModel", client_factory)
    app = bootstrap.build_main_application(tmp_path)
    req = MainTurnRequest.model_validate(
        {"messages": ({"role": "user", "content": "测试"},)}
    )
    ctx = MainTurnContext(
        user_id="local", session_id="local", turn_id="local", request_revision=1
    )
    events = [event async for event in app.astream_turn(req, ctx)]
    assert events[-1].payload.type == "turn.completed"
    assert clients[0].decisions[0][0].content.startswith("决策标记 ")
    assert clients[0].responses[0][0].content.startswith("回复标记 ")
    assert {ref.version for ref in events[-1].prompt_refs} == {
        "test-decision",
        "test-response",
    }


def test_app_rejects_changed_caller_variables_during_assembly(tmp_path):
    """同时改正文与 YAML 变量后，若没有同步调用代码，应在装配时失败。"""
    config = temporary_project(tmp_path)
    config["prompts"]["main_decision"]["required_variables"] = ["other"]
    (tmp_path / "prompts/custom/decision.txt").write_text("{{other}}", encoding="utf-8")
    save_config(tmp_path, config)
    registry = load_prompt_registry(tmp_path)
    model = RecordingModel()
    with pytest.raises(ResearchRuntimeError, match="调用合同"):
        MainAgentApplication(
            model,
            registry.get("main_decision"),
            registry.get("main_response"),
            turn_timeout_seconds=5,
            max_input_characters=32000,
        )
    assert model.decisions == model.responses == []
