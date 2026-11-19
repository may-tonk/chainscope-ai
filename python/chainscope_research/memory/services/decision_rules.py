"""提取与裁决的程序边界：核对用户原文、候选覆盖和已授权版本快照。

这些函数只处理调用方提供的可信快照，不连接数据库或请求模型。
调用方必须从认证及存储取得上下文、来源和检索结果，并在实际提交时再次做事务校验。
"""

from collections.abc import Mapping
from uuid import UUID

from chainscope_research.memory.contracts.common import MemoryUserContext
from chainscope_research.memory.contracts.conversations import MessageRecord
from chainscope_research.memory.contracts.memories import (
    CandidateBatch,
    CandidateFact,
    DecisionBatch,
    MemoryVersionRecord,
    NoopDecision,
    UpdateDecision,
)


def validate_source_evidence(
    candidate: CandidateFact,
    messages: tuple[MessageRecord, ...],
    *,
    latest_user_message_id: UUID,
) -> None:
    """核对候选引文，合法时无返回值；来源越界、非用户原文或缺少本轮依据时失败。

    messages 必须是服务端从当前会话加载的允许来源，而非模型提交的消息记录。
    这里只检查可追溯性，原文是否确实支持候选语义仍需提取与裁决流程判断。
    """
    by_id = {message.message_id: message for message in messages}
    if len(by_id) != len(messages):
        raise ValueError("允许来源中的消息编号不能重复")
    latest = by_id.get(latest_user_message_id)
    if latest is None or latest.role != "user":
        raise ValueError("最新来源必须是已加载的用户消息")
    if not any(
        item.message_id == latest_user_message_id for item in candidate.evidence
    ):
        raise ValueError("候选必须引用当前最新用户消息，不能仅从旧上下文重建")
    for evidence in candidate.evidence:
        source = by_id.get(evidence.message_id)
        if (
            source is None
            or source.role != "user"
            or source.conversation_id != latest.conversation_id
            or evidence.quote not in source.content
        ):
            raise ValueError("候选来源必须是当前会话允许引用的真实用户原文")


def validate_decision_batch(
    candidates: CandidateBatch,
    batch: DecisionBatch,
    *,
    context: MemoryUserContext,
    targets: Mapping[str, tuple[MemoryVersionRecord, ...]],
    suppressed_candidate_ids: frozenset[str] = frozenset(),
    restore_confirmed_ids: frozenset[str] = frozenset(),
) -> None:
    """核对整批裁决，成功无返回值；拒绝漏项、临时固化、越权或错误版本更新。

    targets 按候选绑定服务端检索并核实的旧记忆快照。
    抑制集合和恢复确认集合必须由后续可信业务规则生成，不能从模型字段直接复制。
    本函数只验证已有快照；数据库落库仍须原子核对当前版本、代次及账号状态。
    """
    facts = {fact.candidate_id: fact for fact in candidates.facts}
    if set(facts) != {decision.candidate_id for decision in batch.decisions}:
        raise ValueError("裁决必须恰好覆盖本批所有候选，不能增加或遗漏")
    for decision in batch.decisions:
        candidate = facts[decision.candidate_id]
        if isinstance(decision, NoopDecision):
            continue
        if candidate.scope != "stable":
            raise ValueError("临时或不确定候选只能返回 NOOP")
        suppressed = decision.candidate_id in suppressed_candidate_ids
        if suppressed and not (
            isinstance(decision, UpdateDecision)
            and decision.candidate_id in restore_confirmed_ids
        ):
            raise ValueError("删除抑制生效时只能跳过，明确恢复应更新原记忆的新版本")
        if isinstance(decision, UpdateDecision):
            found = [
                record
                for record in targets.get(decision.candidate_id, ())
                if record.memory_id == decision.target.memory_id
                and record.version == decision.target.version
            ]
            if len(found) != 1:
                raise ValueError("更新目标必须是本候选召回的确定旧版本")
            target = found[0]
            if (
                target.user_id != context.user_id
                or target.memory_generation != context.memory_generation
                or target.status == "superseded"
            ):
                raise ValueError("更新目标归属、记忆代次或状态无效")
            if target.status == "deleted_by_user" and (
                decision.candidate_id not in restore_confirmed_ids
            ):
                raise ValueError("模型不能自行解除用户删除抑制")
            if suppressed and target.status != "deleted_by_user":
                raise ValueError("恢复裁决必须引用实际被删除的目标")
