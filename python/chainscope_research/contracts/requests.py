"""信息提取草稿与服务端执行上下文分离，模型参数中没有身份和权限字段。

本步只声明合同；可信上下文必须由后续认证、授权和任务服务构造与复核。
"""

from typing import Literal, Self

from pydantic import AwareDatetime, model_validator

from chainscope_research.contracts.common import (
    CallIdentity,
    Contract,
    NonEmptyText,
    PositiveInt,
)
from chainscope_research.contracts.periods import TimeSelection
from chainscope_research.contracts.targets import ResearchTarget


class VersionedReference(Contract):
    """引用既有研究或报告必须绑定版本；归属检查由服务层完成。"""

    resource_id: NonEmptyText
    revision: PositiveInt


class ResearchRequestDraft(Contract):
    """提取结果允许未知目标、对象和时间；缺失项不能靠编造补全。

    readiness 只表示意图是否待澄清，不代表工具参数齐全或数据库已有数据。
    具体工具仍使用自己的输入合同，不强制所有问题提供全部字段。
    """

    schema_version: Literal["1.0"] = "1.0"
    user_input: NonEmptyText
    goal: NonEmptyText | None = None
    targets: tuple[ResearchTarget, ...] = ()
    time_selection: TimeSelection | None = None
    context_refs: tuple[VersionedReference, ...] = ()
    assumptions: tuple[NonEmptyText, ...] = ()
    unresolved: tuple[NonEmptyText, ...] = ()
    readiness: Literal["needs_input", "ready"]

    @model_validator(mode="after")
    def check_readiness(self) -> Self:
        """返回语义一致的提取草稿；ready 不能同时声称还有未解决的意图缺口。"""
        if self.readiness == "ready" and (self.goal is None or self.unresolved):
            raise ValueError("ready 必须有研究目标且不存在 unresolved")
        if self.readiness == "needs_input" and not self.unresolved:
            raise ValueError("needs_input 必须说明待澄清的问题")
        return self


class ExecutionContext(CallIdentity):
    """仅供服务端传递的执行关联信息，不作为模型工具 args_schema。

    这些字段能被 Pydantic 解析不代表调用者可信；必须由认证和任务记录取得。
    共享预算、实时权限与资源归属由后续服务实现，不能靠此数据类代替。
    """

    user_id: NonEmptyText
    session_id: NonEmptyText
    deadline_at: AwareDatetime
    # 截止来自现有采集完成记录；查询具体数据源时仍须按该范围约束查询。
    data_cutoff_time: AwareDatetime
