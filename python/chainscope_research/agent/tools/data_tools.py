"""四类真实工具的 LangChain 异步薄适配：授权上下文绑定在服务端闭包中。

Schema 仍从 Pydantic 生成。LangChain 收到的是 JSON schema，执行边界再按同源
Pydantic 模型校验 JSON，避免严格模式下日期字符串、JSON 数组被 Python 校验误拒。
"""

from langchain_core.tools import StructuredTool
from pydantic import ValidationError
from pydantic_core import to_json

from chainscope_research.agent.tools.catalog import TOOL_DEFINITIONS
from chainscope_research.contracts.common import CallIdentity
from chainscope_research.contracts.results import SafeError


async def create_data_tools(
    service, registry, context, *, stop=None
) -> list[StructuredTool]:
    """为已认证调用上下文创建可用工具；模型不能覆盖 user_id、数据范围和期限。

    仅按当前授权与启用状态暴露名称；每次执行仍由服务层重新授权。这里不会
    把工具自动绑定给主 LLM，也不创建专业 subagent；后续节点显式使用返回列表。
    """
    result = []
    for definition in TOOL_DEFINITIONS:
        if not service.settings.tools[definition.tool_id].enabled:
            continue
        grant = await service.authorizer.resolve(context)
        if (
            grant.user_id != context.user_id
            or definition.tool_id not in grant.allowed_tools
            or not grant.allowed_job_ids
        ):
            continue

        def handler_for(item):
            """每个工具绑定自己的合同，避免循环闭包把四种调用全部指向最后一个。"""

            async def invoke(**arguments):
                """先验证真实 JSON 参数，再进入有授权的服务；格式错误不请求数据库。"""
                try:
                    request = item.input_model.model_validate_json(to_json(arguments))
                except (ValidationError, ValueError, TypeError):
                    call = CallIdentity.model_validate(
                        context.model_dump(include=set(CallIdentity.model_fields))
                    )
                    failed = item.output_model(
                        tool_id=item.tool_id,
                        call=call,
                        outcome={
                            "status": "failed",
                            "error": SafeError(
                                code="invalid_input",
                                public_message="参数不符合该工具合同，请检查对象、时间及额外字段。",
                            ),
                        },
                    )
                    return failed.model_dump(mode="json")
                response = await service.execute(
                    item.tool_id, request, context, stop=stop
                )
                return response.model_dump(mode="json")

            return invoke

        result.append(
            StructuredTool.from_function(
                name=definition.tool_id,
                description=definition.read_description(registry),
                args_schema=definition.input_json_schema(),
                coroutine=handler_for(definition),
            )
        )
    return result
