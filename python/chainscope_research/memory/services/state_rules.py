"""Memory 合法状态转换规则；纯函数，不调度任务或修改持久记录。

调用方先处理重复事件，再验证转换，并在数据库中同时比较旧状态、版本和租约。
本模块不能替代事务 CAS、用户归属校验或删除恢复的用户意图确认。
"""

from collections.abc import Mapping

from chainscope_research.memory.contracts.conversations import TurnStatus
from chainscope_research.memory.contracts.jobs import JobStatus, OutboxStatus
from chainscope_research.memory.contracts.memories import MemoryStatus

_TURN = {
    "queued": frozenset({"running", "stopped", "failed"}),
    "running": frozenset({"completed", "stopped", "failed"}),
    "completed": frozenset(),
    "stopped": frozenset(),
    "failed": frozenset(),
}
_MEMORY = {
    "active": frozenset({"superseded", "deleted_by_user"}),
    "superseded": frozenset(),
    "deleted_by_user": frozenset({"active"}),
}
_JOB = {
    "pending": frozenset({"running", "cancelled"}),
    "running": frozenset({"retry_wait", "succeeded", "failed", "cancelled"}),
    "retry_wait": frozenset({"running", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
_OUTBOX = {
    "pending": frozenset({"processing", "obsolete"}),
    "processing": frozenset({"retry_wait", "synced", "failed_sync", "obsolete"}),
    "retry_wait": frozenset({"processing", "failed_sync", "obsolete"}),
    "synced": frozenset(),
    "failed_sync": frozenset({"pending", "obsolete"}),
    "obsolete": frozenset(),
}


def _ensure(current: str, target: str, allowed: Mapping[str, frozenset[str]]) -> None:
    """验证一条转换边；非法、未知和重复转换抛 ValueError，无副作用。

    重复投递应由事件幂等逻辑返回原结果，不能借自转换重复执行写入。
    """
    if target not in allowed.get(current, frozenset()):
        raise ValueError(f"非法状态转换：{current} -> {target}")


def ensure_turn_transition(current: TurnStatus, target: TurnStatus) -> None:
    """验证生成轮次转换；终态保持终态，重试需要新轮次而非复活旧轮。"""
    _ensure(current, target, _TURN)


def ensure_memory_transition(
    current: MemoryStatus, target: MemoryStatus, *, restore_confirmed: bool = False
) -> None:
    """验证记忆状态；恢复删除记忆必须由服务端先确认当前用户的明确意图。

    更新应新建版本并替代旧版，恢复也应写新版本；此处不直接修改历史正文。
    restore_confirmed 不得从模型分类字段或浏览器请求原样透传。
    """
    _ensure(current, target, _MEMORY)
    if current == "deleted_by_user" and not restore_confirmed:
        raise ValueError("恢复已删除记忆需要当前用户明确确认")


def ensure_job_transition(current: JobStatus, target: JobStatus) -> None:
    """验证作业转换；Worker 失败和取消不会改写已经结束的前台回答。"""
    _ensure(current, target, _JOB)


def ensure_outbox_transition(
    current: OutboxStatus, target: OutboxStatus, *, repair_requested: bool = False
) -> None:
    """验证索引同步转换；达到失败终点后，重新排队需要显式修复操作。

    修复服务还需重置或增加执行预算并记录操作，不能让普通 Worker 无限重排。
    """
    _ensure(current, target, _OUTBOX)
    if current == "failed_sync" and target == "pending" and not repair_requested:
        raise ValueError("失败同步事件只能由显式修复入口重新排队")
