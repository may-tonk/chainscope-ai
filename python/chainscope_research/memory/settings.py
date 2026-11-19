"""读取 Memory 非秘密运行设置，返回不可变快照；不读取消息或创建服务连接。

YAML 是默认参数的唯一来源；重复键、未知字段和不合法组合均拒绝。
数据库凭证仍由装配方传入的 profile 提供，不与本文件混合或回显。
"""

from pathlib import Path
from typing import Self

import yaml
from pydantic import Field, model_validator

from chainscope_research.contracts.common import Contract
from chainscope_research.core.settings import UniqueKeyLoader


class DatabaseSettings(Contract):
    """单连接池与 SQL 等待参数，不表示跨实例的全站容量。"""

    pool_size: int = Field(ge=1, le=100)
    max_overflow: int = Field(ge=0, le=100)
    pool_timeout_seconds: int = Field(ge=1, le=120)
    connect_timeout_seconds: int = Field(ge=1, le=120)
    statement_timeout_ms: int = Field(ge=100, le=120000)
    lock_timeout_ms: int = Field(ge=100, le=120000)

    @model_validator(mode="after")
    def check_timeouts(self) -> Self:
        """锁等待应在语句截止前结束，防止配置含义互相冲突。"""
        if self.lock_timeout_ms > self.statement_timeout_ms:
            raise ValueError("锁等待不能超过语句执行上限")
        return self


class HistorySettings(Contract):
    """消息分页与摘要接入前的上下文水位；结构上限仍由数据合同保证。"""

    title_characters: int = Field(ge=1, le=160)
    poll_seconds: int = Field(ge=1, le=30)
    page_size: int = Field(ge=1, le=500)
    max_page_size: int = Field(ge=1, le=500)
    max_context_messages: int = Field(ge=2, le=500)

    @model_validator(mode="after")
    def check_page_size(self) -> Self:
        """默认分页不能超过最大分页值。"""
        if self.page_size > self.max_page_size:
            raise ValueError("默认分页不能超过最大分页大小")
        if self.max_context_messages > self.max_page_size:
            raise ValueError("上下文消息上限不能超过单次内部读取上限")
        return self


class StreamSettings(Contract):
    """正文提交与取消收尾设置；线程槽位在实际操作结束后释放。"""

    flush_characters: int = Field(ge=1, le=10000)
    flush_interval_seconds: float = Field(gt=0, le=5)
    cleanup_timeout_seconds: int = Field(ge=1, le=30)
    database_parallelism: int = Field(ge=1, le=100)


class RecoverySettings(Contract):
    """运行轮次的心跳与过期恢复水位。"""

    heartbeat_seconds: int = Field(ge=1, le=60)
    stale_after_seconds: int = Field(ge=10, le=3600)

    @model_validator(mode="after")
    def check_lease(self) -> Self:
        """预留至少三次心跳间隔，减少网络短暂抖动造成的误判。"""
        if self.stale_after_seconds < 3 * self.heartbeat_seconds:
            raise ValueError("过期水位至少应为心跳间隔的三倍")
        return self


class MemorySettings(Contract):
    """按进程装配并注入的完整 Memory 设置，不在模块全局保存可变用户状态。"""

    database: DatabaseSettings
    history: HistorySettings
    stream: StreamSettings
    recovery: RecoverySettings


def load_memory_settings(root: Path | None = None) -> MemorySettings:
    """读取项目 config/memory/runtime.yaml，成功返回快照，失败只给安全提示。"""
    root = root or Path(__file__).resolve().parents[3]
    try:
        raw = yaml.load(
            (root / "config/memory/runtime.yaml").read_text(encoding="utf-8"),
            Loader=UniqueKeyLoader,
        )
        return MemorySettings.model_validate(raw)
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        raise ValueError(
            "请检查 config/memory/runtime.yaml 的 Memory 运行设置"
        ) from None
