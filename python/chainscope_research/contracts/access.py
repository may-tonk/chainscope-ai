"""研究工具服务端授权接口；模型输入不携带用户权限或可访问的采集任务列表。"""

from typing import Protocol

from chainscope_research.contracts.common import Contract, NonEmptyText
from chainscope_research.contracts.requests import ExecutionContext
from chainscope_research.contracts.tools import ToolId


class ToolGrant(Contract):
    """权限服务根据真实 user_id 返回的授权快照；每次执行前重新获取。"""

    user_id: NonEmptyText
    allowed_job_ids: tuple[NonEmptyText, ...]
    allowed_tools: tuple[ToolId, ...]


class ToolAuthorizer(Protocol):
    """由已认证网站服务注入，不默认放行，也不从用户消息生成授权。"""

    async def resolve(self, context: ExecutionContext) -> ToolGrant:
        """输入可信调用上下文，返回当前用户的能力与数据集访问范围。"""
        ...
