"""工具合同目录：绑定名称、Pydantic 输入输出及独立描述。

本模块只登记合同；真实异步 handler 由同目录 data_tools.py 按授权装配。
导入目录不会查询数据库、调用模型或自动装载环境变量。
"""

from dataclasses import dataclass

from chainscope_research.contracts.common import Contract
from chainscope_research.contracts.results import ToolResult
from chainscope_research.contracts.tools import (
    AddressActivityData,
    AnalyzeAddressActivityInput,
    AnalyzeTokenActivityInput,
    EventLookupData,
    LookupBlockEventsInput,
    LookupTransactionEventsInput,
    TokenActivityData,
    ToolId,
)
from chainscope_research.core.prompt_loader import LoadedPrompt, PromptRegistry


@dataclass(frozen=True)
class ToolDefinition:
    """只描述工具合同；输入不携带权限，描述文件也不能授予权限。"""

    tool_id: ToolId
    input_model: type[Contract]
    output_model: type[ToolResult]
    # 只保存 YAML 中的逻辑名称，不在代码中保存或拼接 TXT 路径。
    description_prompt_name: str

    def description_template(self, registry: PromptRegistry) -> LoadedPrompt:
        """输入已装配的只读目录，按名称取模板；版本和摘要也可由调用方核对。

        工具绑定固定的逻辑名称，具体文件由 YAML 指定。路径安全、空正文等
        由统一加载器检查。当前描述不接受动态变量，工具参数不能混入描述模板。
        """
        if self.description_prompt_name != f"tool_{self.tool_id}":
            raise ValueError("工具描述登记名必须为 tool_ 加 tool_id")
        template = registry.get(self.description_prompt_name)
        template.require_variables(())
        return template

    def read_description(self, registry: PromptRegistry) -> str:
        """从同一加载快照返回静态正文；不自行读盘，不回退到旧的固定路径。"""
        return self.description_template(registry).render().strip()

    def to_model_schema(self, registry: PromptRegistry) -> dict:
        """组合名称、描述和同源参数 Schema，供工具装配检查。

        这里只构造元数据，不注册 handler 或调用模型。实际 handler 必须经
        data_tools.create_data_tools 绑定可信上下文，不能仅凭本定义获得数据权限。
        """
        return {
            "type": "function",
            "function": {
                "name": self.tool_id,
                "description": self.read_description(registry),
                "parameters": self.input_json_schema(),
            },
        }

    def input_json_schema(self) -> dict:
        """返回从 Pydantic 自动生成的参数 schema，不维护第二份手写 JSON。"""
        return self.input_model.model_json_schema()

    def output_json_schema(self) -> dict:
        """返回从同源模型生成的返回 schema；不因此声称已经接通模型供应商。"""
        return self.output_model.model_json_schema()

    def parse_result_json(self, payload: str) -> ToolResult:
        """解析 JSON 返回值并检查工具归属，防止其他工具的结果串入当前调用。"""
        result = self.output_model.model_validate_json(payload)
        if result.tool_id != self.tool_id:
            raise ValueError("工具返回的 tool_id 与当前合同不一致")
        return result


TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        tool_id="lookup_transaction_events",
        input_model=LookupTransactionEventsInput,
        output_model=ToolResult[EventLookupData],
        description_prompt_name="tool_lookup_transaction_events",
    ),
    ToolDefinition(
        tool_id="lookup_block_events",
        input_model=LookupBlockEventsInput,
        output_model=ToolResult[EventLookupData],
        description_prompt_name="tool_lookup_block_events",
    ),
    ToolDefinition(
        tool_id="analyze_address_activity",
        input_model=AnalyzeAddressActivityInput,
        output_model=ToolResult[AddressActivityData],
        description_prompt_name="tool_analyze_address_activity",
    ),
    ToolDefinition(
        tool_id="analyze_token_activity",
        input_model=AnalyzeTokenActivityInput,
        output_model=ToolResult[TokenActivityData],
        description_prompt_name="tool_analyze_token_activity",
    ),
)
