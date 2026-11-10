"""工具状态、分析发现与总结草稿的返回合同；状态和证据不能混为任意字典。

这里只验证结构和引用一致性；事实计算与授权在工具服务执行，主 Agent 审核另行接入。
"""

from typing import Annotated, Generic, Literal, Self, TypeVar

from pydantic import AwareDatetime, Field, model_validator

from chainscope_research.contracts.common import (
    CallIdentity,
    ChainId,
    Contract,
    EvmAddress,
    NonEmptyText,
    PositiveInt,
    UnsignedDecimal,
)
from chainscope_research.contracts.tools import ToolId

Payload = TypeVar("Payload", bound=Contract)
RequiredNotes = Annotated[tuple[NonEmptyText, ...], Field(min_length=1)]


class DatasetScope(Contract):
    """说明实际可用的已索引数据集；不能把某代币采集范围描述成全链覆盖。"""

    dataset_id: NonEmptyText
    chain_id: ChainId
    token_address: EvmAddress
    completed_through_block: UnsignedDecimal
    data_cutoff_time: AwareDatetime
    scope_note: NonEmptyText


class ResultMetadata(Contract):
    """由执行程序附加的关联和来源信息；方法版本用于复核，不代表验证已完成。"""

    result_id: NonEmptyText
    generated_at: AwareDatetime
    method_version: NonEmptyText
    sources: Annotated[tuple[DatasetScope, ...], Field(min_length=1)]


class Success(Contract, Generic[Payload]):
    """成功必须有类型明确的数据；查询返回零条只在此状态中表示真实零结果。"""

    status: Literal["success"]
    data: Payload


class Partial(Contract, Generic[Payload]):
    """可用部分结果必须同时交代局限，后续总结不能把它升级为完整成功。"""

    status: Literal["partial"]
    data: Payload
    limitations: RequiredNotes


class NeedsInput(Contract):
    """缺参进入追问，明确问题；不接收伪造的空数据载荷。"""

    status: Literal["needs_input"]
    questions: RequiredNotes


class Unsupported(Contract):
    """现有能力或数据范围不支持，原因交给主 Agent 解释或调整计划。"""

    status: Literal["unsupported"]
    reason: NonEmptyText


class SafeError(Contract):
    """安全错误码与可公开消息；服务层禁止将原始异常、SQL 或密钥 URL 填入。"""

    code: Literal[
        "invalid_input",
        "permission_denied",
        "resource_unavailable",
        "dependency_unavailable",
        "timeout",
        "budget_exceeded",
        "internal_error",
    ]
    public_message: NonEmptyText
    # 这是错误分类信息；实际是否重试仍由同一研究的共享预算控制。
    retryable: bool = False

    @model_validator(mode="after")
    def check_retry_classification(self) -> Self:
        """返回一致的错误分类；权限拒绝和非法参数不能通过重试绕过。"""
        if self.retryable and self.code in {"invalid_input", "permission_denied"}:
            raise ValueError("权限拒绝和非法参数不可标记为可重试")
        return self


class Failed(Contract):
    """失败只携带安全错误，不允许同时附带一个看似成功的 data。"""

    status: Literal["failed"]
    error: SafeError


class Cancelled(Contract):
    """仅执行方确认停止后使用；收到取消请求不等于任务已经停止。"""

    status: Literal["cancelled"]
    reason: NonEmptyText


class ToolResult(Contract, Generic[Payload]):
    """通用返回信封；outcome 按 status 分支，metadata 仅在已有结果时提供。

    查询失败可能尚未获得数据源，不强制失败方编造数据截止或结果版本。
    """

    schema_version: Literal["1.0"] = "1.0"
    tool_id: ToolId
    call: CallIdentity
    outcome: Annotated[
        Success[Payload]
        | Partial[Payload]
        | NeedsInput
        | Unsupported
        | Failed
        | Cancelled,
        Field(discriminator="status"),
    ]
    metadata: ResultMetadata | None = None

    @model_validator(mode="after")
    def check_metadata(self) -> Self:
        """输入状态与元信息，返回一致合同；有计算结果必须能追溯来源。"""
        if isinstance(self.outcome, (Success, Partial)) and self.metadata is None:
            raise ValueError("success/partial 必须提供结果和数据来源元信息")
        return self


class ResultReference(Contract):
    """分析和总结引用具体结果及请求修订，防止旧结果冒充新问题的答案。"""

    result_id: NonEmptyText
    request_revision: PositiveInt


class Finding(Contract):
    """证据支持的发现必须有结果引用；schema 不替代对陈述真实性的审核。"""

    finding_id: NonEmptyText
    statement: NonEmptyText
    result_refs: Annotated[tuple[ResultReference, ...], Field(min_length=1)]
    limitations: tuple[NonEmptyText, ...] = ()


class Hypothesis(Contract):
    """待检验解释与已确认发现分开，简述缺少的材料，不由模型编造概率。"""

    statement: NonEmptyText
    missing_materials: RequiredNotes


class AnalysisResult(Contract):
    """分析 subagent 给总结方的内容合同；工具数值仍保留在原结果中。"""

    schema_version: Literal["1.0"] = "1.0"
    research_id: NonEmptyText
    request_revision: PositiveInt
    result_refs: tuple[ResultReference, ...]
    findings: tuple[Finding, ...]
    hypotheses: tuple[Hypothesis, ...] = ()
    limitations: tuple[NonEmptyText, ...] = ()
    checks: tuple[NonEmptyText, ...] = ()
    followups: tuple[NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def check_references(self) -> Self:
        """返回引用闭合的分析合同；禁止发现引用未列入当前分析的结果版本。"""
        allowed = set(self.result_refs)
        if len({finding.finding_id for finding in self.findings}) != len(self.findings):
            raise ValueError("finding_id 不能重复")
        for finding in self.findings:
            if not set(finding.result_refs).issubset(allowed):
                raise ValueError("发现引用了当前分析未声明的结果版本")
        return self


class AnswerDraft(Contract):
    """总结到主 Agent 的草稿；主 Agent 仍需审核范围、证据和未回答问题。"""

    schema_version: Literal["1.0"] = "1.0"
    research_id: NonEmptyText
    request_revision: PositiveInt
    direct_answer: NonEmptyText
    findings: tuple[Finding, ...]
    limitations: tuple[NonEmptyText, ...] = ()
    unanswered: tuple[NonEmptyText, ...] = ()
