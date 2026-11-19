"""会话、消息、生成轮次和摘要合同；只校验数据，不读写数据库。

conversation_id 是聊天会话标识，与网站登录 session_id 分开。
浏览器提交新消息；历史、用户身份、消息状态和生成结果由服务端维护。
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from chainscope_research.contracts.common import Contract, PositiveInt
from chainscope_research.memory.contracts.common import (
    NonNegativeInt,
    OriginalText,
    SafeErrorCode,
)

TurnStatus = Literal["queued", "running", "completed", "stopped", "failed"]
StopReason = Literal[
    "user_stop", "navigation", "superseded", "connection_lost", "server_restart"
]


class SendMessageInput(Contract):
    """一条新消息的请求；请求 ID 用于幂等，预期修订号用于发现并发修改。

    不接收 user_id、历史消息、系统角色或生成状态，避免浏览器伪造服务端记录。
    幂等查重和修订号比较需要后续在数据库事务中落实。
    """

    conversation_id: UUID
    client_request_id: UUID
    expected_revision: PositiveInt
    content: OriginalText


class ConversationRecord(Contract):
    """服务端会话快照；用户归属显式保存，revision 随业务变更递增。"""

    conversation_id: UUID
    user_id: UUID
    title: Annotated[str, Field(min_length=1, max_length=160)]
    revision: PositiveInt
    status: Literal["active", "deleted"]
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def check_times(self) -> Self:
        """输入完整快照，返回时间有序的合同；拒绝更新时间早于创建时间。"""
        if self.updated_at < self.created_at:
            raise ValueError("会话更新时间不能早于创建时间")
        if not self.title.strip():
            raise ValueError("会话标题不能全为空白")
        return self


class TurnRecord(Contract):
    """一轮生成的状态快照；终态必须有结束时间，停止和失败保留具体原因。"""

    conversation_id: UUID
    turn_id: UUID
    request_revision: PositiveInt
    client_request_id: UUID
    status: TurnStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    stop_reason: StopReason | None = None
    error_code: SafeErrorCode | None = None

    @model_validator(mode="after")
    def check_lifecycle(self) -> Self:
        """校验状态、原因和时间的一致性；返回快照，不改变任何运行状态。"""
        terminal = self.status in {"completed", "stopped", "failed"}
        if terminal != (self.finished_at is not None):
            raise ValueError("只有已完成、已停止或失败的轮次必须填写结束时间")
        if (self.status == "stopped") != (self.stop_reason is not None):
            raise ValueError("只有 stopped 轮次必须填写停止原因")
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("只有 failed 轮次必须填写安全错误码")
        if self.updated_at < self.created_at:
            raise ValueError("轮次更新时间不能早于创建时间")
        if self.finished_at is not None and not (
            self.created_at <= self.finished_at <= self.updated_at
        ):
            raise ValueError("轮次结束时间必须位于创建和更新时间之间")
        return self


class MessageRecord(Contract):
    """消息存储快照；助手允许空占位和半截内容，完成正文必须非空。"""

    message_id: UUID
    conversation_id: UUID
    turn_id: UUID
    sequence: PositiveInt
    role: Literal["user", "assistant"]
    status: TurnStatus
    content: Annotated[str, Field(max_length=100000)]
    created_at: AwareDatetime

    @model_validator(mode="after")
    def check_message_state(self) -> Self:
        """拒绝未完成的用户原文和空的完成正文；原文保留空格与换行。"""
        if self.role == "user" and self.status != "completed":
            raise ValueError("已接收的用户消息必须是 completed")
        if self.status == "completed" and not self.content.strip():
            raise ValueError("完成的消息正文不能为空")
        return self


class SessionSummaryRecord(Contract):
    """可追溯摘要；原文仍在消息表，覆盖位置不由摘要自然语言决定。"""

    summary_id: UUID
    conversation_id: UUID
    version: PositiveInt
    based_on_version: NonNegativeInt
    covered_until_message_id: UUID
    covered_until_sequence: PositiveInt
    content: OriginalText
    created_at: AwareDatetime

    @model_validator(mode="after")
    def check_revision(self) -> Self:
        """摘要必须从明确旧版本递增；实际防止迟到覆盖仍需数据库 CAS。"""
        if self.version != self.based_on_version + 1:
            raise ValueError("摘要版本必须在基础版本上递增一")
        return self


class TurnAcceptance(Contract):
    """一次发送事务的完整结果；重复请求返回同一轮和消息，并标记 replayed。"""

    conversation: ConversationRecord
    turn: TurnRecord
    user_message: MessageRecord
    assistant_message: MessageRecord
    replayed: bool
