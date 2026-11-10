"""工具目录测试：描述与合同一一绑定，生成 schema 且不会注册虚假执行能力。"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from chainscope_research.agent.tools.catalog import TOOL_DEFINITIONS
from chainscope_research.core.errors import ResearchRuntimeError
from chainscope_research.core.prompt_loader import load_prompt_registry

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("definition", TOOL_DEFINITIONS, ids=lambda item: item.tool_id)
def test_tool_description_and_generated_schema(definition):
    """每个工具独占描述目录；输入禁止额外字段，返回状态通过 discriminator 区分。"""
    registry = load_prompt_registry(PROJECT_ROOT)
    description = definition.read_description(registry)
    assert description.startswith(f"工具：{definition.tool_id}")
    assert (
        PROJECT_ROOT / "prompts" / "tools" / definition.tool_id / "description.txt"
    ).is_file()
    schema = definition.input_json_schema()
    assert schema["additionalProperties"] is False
    assert "user_id" not in schema["properties"]
    output_schema = definition.output_json_schema()
    assert (
        output_schema["properties"]["outcome"]["discriminator"]["propertyName"]
        == "status"
    )
    # 当前只提供合同，不能把目录里的工具名称当成已具备执行 handler。
    assert not hasattr(definition, "handler")
    # 模型可见的描述来源与 Pydantic 参数应一一绑定；生成定义不等于启用执行。
    model_schema = definition.to_model_schema(registry)
    assert model_schema["function"] == {
        "name": definition.tool_id,
        "description": description,
        "parameters": schema,
    }
    template = definition.description_template(registry)
    assert template.reference.name == f"tool_{definition.tool_id}"
    assert template.required_variables == ()


def temporary_tool_project(
    tmp_path, *, content="自定义工具描述", name=None, variables=None
):
    """创建只含一份描述的临时登记；使用 alternate.txt 证明不再依赖固定文件名。"""
    definition = TOOL_DEFINITIONS[0]
    (tmp_path / "config").mkdir()
    directory = tmp_path / "prompts" / "tools" / definition.tool_id
    directory.mkdir(parents=True)
    file = directory / "alternate.txt"
    file.write_text(content, encoding="utf-8")
    config = {
        "prompts": {
            name or definition.description_prompt_name: {
                "file": f"prompts/tools/{definition.tool_id}/alternate.txt",
                "version": "custom",
                "required_variables": variables or [],
            }
        }
    }
    (tmp_path / "config/research_prompts.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )
    return file


@pytest.mark.parametrize(
    "case", ["missing_registration", "missing_file", "blank", "dynamic_variables"]
)
def test_missing_blank_or_dynamic_description_fails(tmp_path, case):
    """缺登记、缺文件、空正文和不符合静态合同的变量都失败，不回退到旧路径。"""
    definition = TOOL_DEFINITIONS[0]
    file = temporary_tool_project(
        tmp_path,
        name="other" if case == "missing_registration" else None,
        content=" \n"
        if case == "blank"
        else "描述 {{extra}}"
        if case == "dynamic_variables"
        else "描述",
        variables=["extra"] if case == "dynamic_variables" else [],
    )
    if case == "missing_file":
        file.unlink()
    with pytest.raises(ResearchRuntimeError) as caught:
        registry = load_prompt_registry(tmp_path)
        definition.read_description(registry)
    expected = (
        "PROMPT_NOT_REGISTERED"
        if case == "missing_registration"
        else "PROMPT_VARIABLES_INVALID"
        if case == "dynamic_variables"
        else "PROMPT_CONFIG_INVALID"
    )
    assert caught.value.code == expected


def test_mismatched_description_binding_fails():
    """工具不得误绑定成主 Agent 正文提示词，即使该名字确实已经登记。"""
    definition = replace(TOOL_DEFINITIONS[0], description_prompt_name="main_response")
    with pytest.raises(ValueError, match="登记名"):
        definition.read_description(load_prompt_registry(PROJECT_ROOT))


def test_yaml_path_changes_and_snapshot_versions_are_respected(tmp_path):
    """修改 YAML 所指文件可以更换描述；已装配快照不热更新，新加载才生效。"""
    file = temporary_tool_project(tmp_path, content="自定义的工具描述")
    registry = load_prompt_registry(tmp_path)
    definition = TOOL_DEFINITIONS[0]
    assert not (file.parent / "description.txt").exists()
    assert definition.read_description(registry) == "自定义的工具描述"
    first = definition.description_template(registry).reference
    assert first.version == "custom"
    file.write_text("更新后的描述", encoding="utf-8")
    reloaded = load_prompt_registry(tmp_path)
    assert definition.read_description(registry) == "自定义的工具描述"
    assert definition.read_description(reloaded) == "更新后的描述"
    assert (
        definition.description_template(reloaded).reference.content_sha256
        != first.content_sha256
    )


def test_description_and_tool_schema_calls_do_not_mutate_registry():
    """调用工具定义构造接口不修改公共模板，仍保留六份独立登记资源。"""
    registry = load_prompt_registry(PROJECT_ROOT)
    assert set(registry.entries) == {
        "main_decision",
        "main_response",
        *(f"tool_{definition.tool_id}" for definition in TOOL_DEFINITIONS),
    }
    for definition in TOOL_DEFINITIONS:
        before = definition.description_template(registry)
        definition.to_model_schema(registry)
        assert definition.description_template(registry) is before


def test_tool_names_unique_and_result_identity_checked():
    """工具名不能重复；同载荷类型的交易与区块查询也不能交叉接收结果。"""
    assert len({definition.tool_id for definition in TOOL_DEFINITIONS}) == len(
        TOOL_DEFINITIONS
    )
    payload = {
        "tool_id": "lookup_block_events",
        "call": {
            "research_id": "test-research",
            "request_revision": 1,
            "task_id": "test-task",
            "attempt_id": "test-attempt",
            "call_id": "test-call",
        },
        "outcome": {"status": "unsupported", "reason": "测试用未实现范围"},
    }
    with pytest.raises(ValueError, match="不一致"):
        TOOL_DEFINITIONS[0].parse_result_json(json.dumps(payload))
