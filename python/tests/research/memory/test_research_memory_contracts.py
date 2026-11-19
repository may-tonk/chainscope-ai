"""Memory 基础边界验证：使用本地构造数据，不访问服务或调用供应商。

重点验证身份注入、错误来源、过期版本、删除恢复、生成终态及作业预算，
不把这些本地检查当成数据库并发、索引一致性或真实模型效果的验收。
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from chainscope_research.memory.contracts.common import (
    MemoryUserContext,
    MemoryVersionReference,
)
from chainscope_research.memory.contracts.conversations import (
    MessageRecord,
    SendMessageInput,
    SessionSummaryRecord,
    TurnRecord,
)
from chainscope_research.memory.contracts.jobs import (
    MemoryJobRecord,
    MemoryOutboxRecord,
)
from chainscope_research.memory.contracts.memories import (
    CandidateBatch,
    CandidateFact,
    DecisionBatch,
    MemoryMetadata,
    MemoryVersionRecord,
)
from chainscope_research.memory.services.decision_rules import (
    validate_decision_batch,
    validate_source_evidence,
)
from chainscope_research.memory.services.state_rules import (
    ensure_job_transition,
    ensure_memory_transition,
    ensure_outbox_transition,
    ensure_turn_transition,
)

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)
USER, CONVERSATION, TURN, MESSAGE, MEMORY = (uuid4() for _ in range(5))
CONTENT = {"text": "默认使用北京时间", "category": "preference"}


def candidate(**changes):
    """返回有最新用户引文的虚构候选，允许每个案例覆盖一个待验证边界。"""
    return CandidateFact.model_validate(
        {
            "candidate_id": "c1",
            "content": CONTENT,
            "scope": "stable",
            "evidence": ({"message_id": MESSAGE, "quote": "以后默认使用北京时间"},),
            **changes,
        }
    )


def memory(**changes):
    """返回受信检索快照样例，明确所有者、代次和版本，不读实际数据库。"""
    return MemoryVersionRecord.model_validate(
        {
            "memory_id": MEMORY,
            "version": 1,
            "user_id": USER,
            "memory_generation": 0,
            "status": "active",
            "content": CONTENT,
            "origin": "automatic",
            "source_message_ids": (MESSAGE,),
            "created_at": NOW,
            **changes,
        }
    )


def update_batch(**changes):
    """构造一个结构化 UPDATE 裁决；目标必须由后续规则核对，不能只看格式合法。"""
    return DecisionBatch.model_validate(
        {
            "decisions": (
                {
                    "action": "UPDATE",
                    "candidate_id": "c1",
                    "target": {"memory_id": MEMORY, "version": 1},
                    "content": CONTENT,
                    "reason": "用户更正偏好",
                    **changes,
                },
            )
        }
    )


@pytest.mark.parametrize("field", ["user_id", "messages", "status", "system_prompt"])
def test_browser_cannot_supply_server_state(field):
    """真实 JSON 边界拒绝身份、历史与状态注入；合法原文保留空格和换行。"""
    body = {
        "conversation_id": str(CONVERSATION),
        "client_request_id": str(uuid4()),
        "expected_revision": 1,
        "content": "  原始问题\n",
    }
    assert (
        SendMessageInput.model_validate_json(json.dumps(body)).content == "  原始问题\n"
    )
    with pytest.raises(ValidationError):
        SendMessageInput.model_validate_json(json.dumps({**body, field: "forged"}))


@pytest.mark.parametrize("revision", [True, "1", 0])
def test_revision_is_a_strict_positive_integer(revision):
    """布尔值、字符串和零不能冒充修订号，避免并发判断读取含糊数据。"""
    with pytest.raises(ValidationError):
        SendMessageInput(
            conversation_id=CONVERSATION,
            client_request_id=uuid4(),
            expected_revision=revision,
            content="问题",
        )


@pytest.mark.parametrize(
    "state,reason,error",
    [
        ("completed", None, None),
        ("stopped", "navigation", None),
        ("failed", None, "TIMEOUT"),
    ],
)
def test_terminal_turn_requires_consistent_reason_and_time(state, reason, error):
    """终态必须保存结束时间；停止和失败有各自依据，不能伪装为完成。"""
    body = {
        "conversation_id": CONVERSATION,
        "turn_id": TURN,
        "request_revision": 1,
        "client_request_id": uuid4(),
        "status": state,
        "created_at": NOW,
        "updated_at": NOW,
        "stop_reason": reason,
        "error_code": error,
    }
    with pytest.raises(ValidationError):
        TurnRecord.model_validate(body)
    assert TurnRecord.model_validate({**body, "finished_at": NOW}).status == state


def test_partial_assistant_is_preserved_but_empty_success_is_rejected():
    """允许停止时没有正文，拒绝把空回答保存成成功；不修改已有片段。"""
    body = dict(
        message_id=MESSAGE,
        conversation_id=CONVERSATION,
        turn_id=TURN,
        sequence=2,
        role="assistant",
        created_at=NOW,
        content="",
        status="stopped",
    )
    assert MessageRecord.model_validate(body).content == ""
    with pytest.raises(ValidationError):
        MessageRecord.model_validate({**body, "status": "completed"})


def test_summary_keeps_explicit_coverage_and_contiguous_revision():
    """摘要必须记录明确的原文覆盖位置，不能越过基础版本写入。"""
    body = dict(
        summary_id=uuid4(),
        conversation_id=CONVERSATION,
        version=2,
        based_on_version=1,
        covered_until_message_id=MESSAGE,
        covered_until_sequence=24,
        content="早期讨论摘要",
        created_at=NOW,
    )
    assert SessionSummaryRecord.model_validate(body).covered_until_sequence == 24
    with pytest.raises(ValidationError):
        SessionSummaryRecord.model_validate({**body, "version": 3})
    with pytest.raises(ValidationError):
        SessionSummaryRecord.model_validate(
            {**body, "created_at": NOW.replace(tzinfo=None)}
        )


def test_entity_keys_bind_chain_and_normalize_only_evm_addresses():
    """相同地址在不同链有不同联合键；避免将两组独立数组误配成所有链。"""
    address = "0x" + "Ab" * 20
    meta = MemoryMetadata.model_validate(
        {
            "chains": ("ethereum", "arbitrum"),
            "entities": (
                {"chain": "ethereum", "address": address},
                {"chain": "arbitrum", "address": address},
            ),
        }
    )
    assert meta.addresses == (address.lower(),)
    assert meta.entity_keys == (
        f"ethereum:{address.lower()}",
        f"arbitrum:{address.lower()}",
    )
    with pytest.raises(ValidationError):
        MemoryMetadata.model_validate(
            {
                "chains": ("ethereum",),
                "entities": ({"chain": "arbitrum", "address": address},),
            }
        )


def test_point_identity_is_repeatable_and_version_specific():
    """同版本重试复用 Point，不同版本绝不共享 Point，避免旧任务覆盖新向量。"""
    old = MemoryVersionReference(memory_id=MEMORY, version=1)
    assert old.point_id == MemoryVersionReference(memory_id=MEMORY, version=1).point_id
    assert old.point_id != MemoryVersionReference(memory_id=MEMORY, version=2).point_id


def test_model_json_cannot_inject_owner_or_hide_writes_in_noop():
    """裁决 JSON 可正确往返，但身份字段和 NOOP 中的隐藏写入内容必须拒绝。"""
    body = json.loads(update_batch().model_dump_json())
    assert DecisionBatch.model_validate_json(json.dumps(body)) == update_batch()
    body["decisions"][0]["user_id"] = str(USER)
    with pytest.raises(ValidationError):
        DecisionBatch.model_validate_json(json.dumps(body))
    with pytest.raises(ValidationError):
        DecisionBatch.model_validate_json(
            json.dumps(
                {
                    "decisions": [
                        {
                            "action": "NOOP",
                            "candidate_id": "c1",
                            "reason": "重复",
                            "content": CONTENT,
                        }
                    ]
                }
            )
        )


@pytest.mark.parametrize(
    "role,quote", [("assistant", "以后默认使用北京时间"), ("user", "不存在的原文")]
)
def test_evidence_cannot_use_assistant_or_fabricated_quote(role, quote):
    """模型不能把助手输出或捏造引文当作用户确认的偏好。"""
    source = MessageRecord(
        message_id=MESSAGE,
        conversation_id=CONVERSATION,
        turn_id=TURN,
        sequence=1,
        role=role,
        status="completed",
        content="以后默认使用北京时间",
        created_at=NOW,
    )
    fact = candidate(evidence=({"message_id": MESSAGE, "quote": quote},))
    with pytest.raises(ValueError):
        validate_source_evidence(fact, (source,), latest_user_message_id=MESSAGE)


def test_old_context_alone_cannot_rebuild_memory():
    """候选即使引用真实旧消息，也必须有当前最新用户消息的支持。"""
    latest_id = uuid4()
    sources = tuple(
        MessageRecord(
            message_id=message_id,
            conversation_id=CONVERSATION,
            turn_id=TURN,
            sequence=index,
            role="user",
            status="completed",
            content="以后默认使用北京时间",
            created_at=NOW,
        )
        for index, message_id in enumerate((MESSAGE, latest_id), start=1)
    )
    validate_source_evidence(candidate(), sources, latest_user_message_id=MESSAGE)
    with pytest.raises(ValueError):
        validate_source_evidence(candidate(), sources, latest_user_message_id=latest_id)


@pytest.mark.parametrize(
    "change", [{"user_id": uuid4()}, {"memory_generation": 1}, {"status": "superseded"}]
)
def test_update_rejects_foreign_stale_or_superseded_snapshot(change):
    """格式合法的 UPDATE 也必须属于当前用户、当前代次和有效版本。"""
    with pytest.raises(ValueError):
        validate_decision_batch(
            CandidateBatch(facts=(candidate(),)),
            update_batch(),
            context=MemoryUserContext(user_id=USER, memory_generation=0),
            targets={"c1": (memory(**change),)},
        )


def test_decision_must_cover_candidates_and_choose_a_retrieved_version():
    """拒绝漏裁决和模型凭空生成目标版本；有效目标可通过。"""
    facts = CandidateBatch(facts=(candidate(),))
    context = MemoryUserContext(user_id=USER, memory_generation=0)
    targets = {"c1": (memory(),)}
    validate_decision_batch(facts, update_batch(), context=context, targets=targets)
    with pytest.raises(ValueError):
        validate_decision_batch(
            facts, DecisionBatch(decisions=()), context=context, targets=targets
        )
    with pytest.raises(ValueError):
        validate_decision_batch(
            facts,
            update_batch(target={"memory_id": MEMORY, "version": 2}),
            context=context,
            targets=targets,
        )


def test_temporary_fact_and_deleted_memory_cannot_be_automatically_written():
    """一次性指令不能固化；模型显式记住标记也不能解除删除抑制。"""
    context = MemoryUserContext(user_id=USER, memory_generation=0)
    with pytest.raises(ValueError):
        validate_decision_batch(
            CandidateBatch(facts=(candidate(scope="temporary"),)),
            update_batch(),
            context=context,
            targets={"c1": (memory(),)},
        )
    facts = CandidateBatch(facts=(candidate(explicit_remember=True),))
    targets = {"c1": (memory(status="deleted_by_user"),)}
    with pytest.raises(ValueError):
        validate_decision_batch(facts, update_batch(), context=context, targets=targets)
    validate_decision_batch(
        facts,
        update_batch(),
        context=context,
        targets=targets,
        suppressed_candidate_ids=frozenset({"c1"}),
        restore_confirmed_ids=frozenset({"c1"}),
    )


@pytest.mark.parametrize("status", ["completed", "stopped", "failed"])
def test_old_turn_cannot_resume_after_terminal_status(status):
    """用户重试要创建新轮，旧流不能把已经结束的轮次改回 running。"""
    with pytest.raises(ValueError):
        ensure_turn_transition(status, "running")


def test_delete_restore_and_outbox_repair_need_explicit_service_confirmation():
    """普通 Worker 不能恢复删除偏好或无限重排失败同步事件。"""
    with pytest.raises(ValueError):
        ensure_memory_transition("deleted_by_user", "active")
    ensure_memory_transition("deleted_by_user", "active", restore_confirmed=True)
    with pytest.raises(ValueError):
        ensure_outbox_transition("failed_sync", "pending")
    ensure_outbox_transition("failed_sync", "pending", repair_requested=True)
    with pytest.raises(ValueError):
        ensure_job_transition("succeeded", "running")


def test_job_lease_and_retry_budget_are_coherent():
    """执行态缺租约、预算耗尽仍重试、终态携带租约均必须拒绝。"""
    body = dict(
        job_id=uuid4(),
        user_id=USER,
        conversation_id=CONVERSATION,
        memory_generation=0,
        kind="extract_memory",
        source_turn_id=TURN,
        status="running",
        attempt_count=1,
        max_attempts=2,
    )
    with pytest.raises(ValidationError):
        MemoryJobRecord.model_validate(body)
    lease = {
        "token": uuid4(),
        "worker_id": "offline-test",
        "expires_at": NOW + timedelta(seconds=30),
    }
    assert MemoryJobRecord.model_validate({**body, "lease": lease}).lease is not None
    with pytest.raises(ValidationError):
        MemoryJobRecord.model_validate({**body, "status": "succeeded", "lease": lease})
    with pytest.raises(ValidationError):
        MemoryJobRecord.model_validate(
            {
                **body,
                "status": "retry_wait",
                "attempt_count": 2,
                "next_retry_at": NOW,
                "last_error_code": "DEPENDENCY_TIMEOUT",
            }
        )


def test_outbox_tombstone_is_distinct_from_physical_purge():
    """删除抑制与物理清理有不同操作名，失败同步必须保留错误依据。"""
    body = dict(
        event_id=uuid4(),
        user_id=USER,
        memory_generation=0,
        target={"memory_id": MEMORY, "version": 1},
        operation="tombstone",
        status="failed_sync",
        attempt_count=2,
        max_attempts=2,
    )
    with pytest.raises(ValidationError):
        MemoryOutboxRecord.model_validate(body)
    assert (
        MemoryOutboxRecord.model_validate(
            {**body, "last_error_code": "INDEX_TIMEOUT"}
        ).operation
        == "tombstone"
    )
    with pytest.raises(ValidationError):
        MemoryOutboxRecord.model_validate(
            {**body, "last_error_code": "https://example.invalid/private?token=value"}
        )
