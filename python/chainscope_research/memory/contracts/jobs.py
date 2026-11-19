"""Memory 作业和索引 Outbox 合同，区分业务执行与索引同步状态。

这里只校验快照一致性；任务领取、租约续期、退避时间和并发比较由后续存储事务实现。
attempt_count 表示包含首次在内的已领取次数，max_attempts 是总次数上限。
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, model_validator

from chainscope_research.contracts.common import Contract, PositiveInt
from chainscope_research.memory.contracts.common import (
    MemoryVersionReference,
    NonNegativeInt,
    SafeErrorCode,
    ShortText,
)

JobStatus = Literal[
    "pending", "running", "retry_wait", "succeeded", "failed", "cancelled"
]
OutboxStatus = Literal[
    "pending", "processing", "retry_wait", "synced", "failed_sync", "obsolete"
]


class WorkLease(Contract):
    """一次领取的执行身份；租约令牌用于拒绝旧 Worker 的迟到写回。"""

    token: UUID
    worker_id: ShortText
    expires_at: AwareDatetime


class RetryMetadata(Contract):
    """共享执行元数据；错误只保存安全码，不接收驱动异常或含秘密的原文。"""

    attempt_count: NonNegativeInt
    max_attempts: PositiveInt
    lease: WorkLease | None = None
    next_retry_at: AwareDatetime | None = None
    last_error_code: SafeErrorCode | None = None

    def check_work_state(self, status: str, *, running: str, success: str) -> None:
        """校验派生记录的执行元数据；失败抛 ValueError，无返回值及 IO。

        运行态必须持有租约；等待重试必须有时间、原因和剩余次数。
        此处不读取系统时间，租约是否已过期由执行服务使用明确时刻判断。
        """
        if self.attempt_count > self.max_attempts:
            raise ValueError("已执行次数不能超过总次数上限")
        if (status == running) != (self.lease is not None):
            raise ValueError("只有执行中的记录必须持有租约")
        if status in {running, success, "retry_wait"} and self.attempt_count == 0:
            raise ValueError("执行、成功或等待重试必须至少领取过一次")
        if status == "retry_wait":
            if (
                self.next_retry_at is None
                or self.last_error_code is None
                or self.attempt_count >= self.max_attempts
            ):
                raise ValueError("等待重试必须有时间、错误码和剩余次数")
        elif self.next_retry_at is not None:
            raise ValueError("只有等待重试的记录可填写下次重试时间")
        if status in {"failed", "failed_sync"} and self.last_error_code is None:
            raise ValueError("失败记录必须保留安全错误码")
        # 已领取的最后一次仍允许执行，但未领取的 pending 不能在次数耗尽后继续排队。
        if status == "pending" and self.attempt_count >= self.max_attempts:
            raise ValueError("次数耗尽后不能继续排队")


class MemoryJobRecord(RetryMetadata):
    """提取或摘要作业快照；来源轮次明确，后台状态独立于前台聊天终态。"""

    job_id: UUID
    user_id: UUID
    conversation_id: UUID
    memory_generation: NonNegativeInt
    kind: Literal["extract_memory", "summarize_session"]
    source_turn_id: UUID | None = None
    status: JobStatus

    @model_validator(mode="after")
    def check_job(self) -> Self:
        """校验来源及执行状态；提取必须指明来源轮次，是否已完成由领取服务核对。"""
        if (self.kind == "extract_memory") != (self.source_turn_id is not None):
            raise ValueError("只有记忆提取作业必须绑定 source_turn_id")
        self.check_work_state(self.status, running="running", success="succeeded")
        return self


class MemoryOutboxRecord(RetryMetadata):
    """记忆索引事件快照；墓碑同步与物理清理使用不同操作，避免混淆删除语义。"""

    event_id: UUID
    user_id: UUID
    memory_generation: NonNegativeInt
    target: MemoryVersionReference
    operation: Literal["upsert", "tombstone", "purge"]
    status: OutboxStatus

    @model_validator(mode="after")
    def check_outbox(self) -> Self:
        """返回执行元数据一致的索引事件；不调用 Qdrant 或变更同步状态。"""
        self.check_work_state(self.status, running="processing", success="synced")
        return self
