"""Agents SDK function adapters with one canonical input/output contract."""

import asyncio
import json
from dataclasses import dataclass

from agents import FunctionTool

from .agent_contracts import INPUTS
from .token_registry import AgentError

DESCRIPTIONS = {
    "get_collection_status": "查看服务列表内代币的本地进度；latest_finalized 额外查询最终确认高度。不启动采集。",
    "prepare_collection": "准备不可变采集计划。用户指定时间用 time + end_time（ISO8601 带时区）；未指定时刻用 latest_finalized；resume 保留原目标。时间以原始用户消息为准，不自行扩大范围。",
    "start_collection": "启动已准备的具体计划。用户明确采集指令在既定范围和预算内直接执行，本地程序绑定授权，无需用户输入编号。重复调用返回同一运行；不修改目标或绕过校验。",
    "get_collection_run": "按运行编号查询实际进度、停止状态和结果。已启动不等于已完成。",
    "stop_collection": "对明确运行请求安全停止，需要本地停止授权；停止请求不等于已经停止。",
    "get_collection_event": "按 event_id 查询当次结束时的不可变结果及已保存报告。历史快照不会因续采而改变。不启动采集。",
    "list_collection_runs": "分页查询当前身份的历史运行及已保存状态，实时进程状态另查 get_collection_run。created_from/to 是运行创建时间（含起点、不含终点），必须带时区；不是链上数据截止时间。next_cursor 填入 before_run_id。",
}


@dataclass
class ToolContext:
    service: object
    operation_id: str
    read_only: bool = False
    allowed_tools: frozenset[str] | None = None
    report_event_id: int | None = None
    authorization: object | None = None


def make_tools(*, read_only=False, names=None):
    def make(name, model):
        async def invoke(context, arguments):
            allowed = context.context.allowed_tools
            if (allowed is not None and name not in allowed) or (
                allowed is None
                and context.context.read_only
                and name != "get_collection_status"
            ):
                return context.context.service.failure(
                    "EXECUTION_DISABLED", "当前对话轮未授权调用这个工具", "none"
                ).model_dump_json()
            try:
                parsed = json.loads(arguments)
            except (ValueError, TypeError):
                return context.context.service.failure(
                    "INVALID_INPUT", "工具参数不是有效 JSON", "none"
                ).model_dump_json()
            if not isinstance(parsed, dict):
                return context.context.service.failure(
                    "INVALID_INPUT", "工具参数必须是 JSON 对象", "none"
                ).model_dump_json()
            if context.context.report_event_id is not None and (
                name != "get_collection_event"
                or parsed.get("event_id") != context.context.report_event_id
            ):
                return context.context.service.failure(
                    "REPORT_SCOPE_MISMATCH", "报告只能查询绑定的结束事件", "none"
                ).model_dump_json()
            authorization = context.context.authorization
            if authorization is not None and name == "prepare_collection":
                try:
                    parsed = authorization.prepare_args(parsed)
                except AgentError as exc:
                    return context.context.service.failure(
                        exc.code, str(exc), "none"
                    ).model_dump_json()
            result = await asyncio.to_thread(
                context.context.service.call,
                name,
                parsed,
                context.context.operation_id,
                authorization=authorization,
            )
            if authorization is not None:
                authorization.observe(result)
            return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

        return FunctionTool(
            name=name,
            description=DESCRIPTIONS[name],
            params_json_schema=model.model_json_schema(),
            # DeepSeek's standard endpoint isn't its beta strict endpoint. Local validation remains strict.
            strict_json_schema=False,
            on_invoke_tool=invoke,
        )

    return [
        make(name, model)
        for name, model in INPUTS.items()
        if (not read_only or name == "get_collection_status")
        and (names is None or name in names)
    ]
