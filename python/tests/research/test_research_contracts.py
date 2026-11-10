"""合同边界测试：拒绝身份注入、错误参数组合及不精确数值；全部使用本地样例。

测试不请求 MySQL/PostgreSQL/Redis，不访问链节点，不调用任何模型。
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from chainscope_research.agent.tools.catalog import TOOL_DEFINITIONS
from chainscope_research.contracts.periods import (
    AbsoluteTimeSelection,
    RelativeTimeSelection,
    TimePeriod,
    resolve_time_selection,
)
from chainscope_research.contracts.requests import ResearchRequestDraft
from chainscope_research.contracts.results import AnalysisResult, SafeError, ToolResult
from chainscope_research.contracts.targets import ResearchTarget
from chainscope_research.contracts.tools import (
    AmountMetric,
    AnalyzeAddressActivityInput,
    AnalyzeTokenActivityInput,
    CountMetric,
    EventLookupData,
    LookupBlockEventsInput,
    LookupTransactionEventsInput,
    RatioMetric,
    TokenActivityData,
)

ADDRESS = "0x" + "1" * 40
TOKEN = "0x" + "2" * 40
HASH = "0x" + "a" * 64
PERIOD = {"start": "2026-11-08T00:00:00+08:00", "end": "2026-11-09T00:00:00+08:00"}
CALL = {
    "research_id": "test-research",
    "request_revision": 1,
    "task_id": "test-task",
    "attempt_id": "test-attempt",
    "call_id": "test-call",
}
TOOL_CASES = (
    (
        LookupTransactionEventsInput,
        {"chain_id": 56, "tx_hash": HASH},
        ("chain_id", "tx_hash"),
    ),
    (
        LookupBlockEventsInput,
        {"chain_id": 56, "block_hash": HASH},
        ("chain_id", "block_hash"),
    ),
    (
        AnalyzeTokenActivityInput,
        {"chain_id": 56, "token_address": TOKEN, "periods": {"focus_period": PERIOD}},
        ("chain_id", "token_address", "periods"),
    ),
    (
        AnalyzeAddressActivityInput,
        {
            "chain_id": 56,
            "token_address": TOKEN,
            "address": ADDRESS,
            "periods": {"focus_period": PERIOD},
        },
        ("chain_id", "token_address", "address", "periods"),
    ),
)


def sample_metadata() -> dict:
    """返回完全虚构的本地结果来源样例，用于检查状态组合，不作为真实业务证据。"""
    return {
        "result_id": "test-result",
        "generated_at": "2026-11-09T00:00:00+08:00",
        "method_version": "test-only",
        "sources": [
            {
                "dataset_id": "test-dataset",
                "chain_id": 56,
                "token_address": TOKEN,
                "completed_through_block": "123",
                "data_cutoff_time": PERIOD["end"],
                "scope_note": "本地虚构样例，不代表真实数据覆盖",
            }
        ],
    }


@pytest.mark.parametrize(("model", "payload", "required"), TOOL_CASES)
def test_tool_requiredness_and_server_fields(model, payload, required):
    """执行最小请求可通过；逐项缺参失败；任何工具都拒绝模型注入服务端字段。"""
    model.model_validate_json(json.dumps(payload))
    for field in required:
        incomplete = {key: value for key, value in payload.items() if key != field}
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps(incomplete))
    for forbidden in (
        "user_id",
        "permissions",
        "data_cutoff_time",
        "sql",
        "code",
        "call_id",
    ):
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps({**payload, forbidden: "forged"}))


@pytest.mark.parametrize("chain_id", [True, "56", 0, -1, 56.0])
def test_chain_id_does_not_coerce_types(chain_id):
    """链编号必须是正整数，不能把布尔值、浮点数或字符串偷偷转换为链。"""
    with pytest.raises(ValidationError):
        LookupTransactionEventsInput.model_validate_json(
            json.dumps({"chain_id": chain_id, "tx_hash": HASH})
        )


def test_hash_semantics_are_explicit():
    """同形哈希按 kind 选择合同；交易与区块字段互换必须拒绝，不能靠长度猜测。"""
    adapter = TypeAdapter(ResearchTarget)
    transaction = adapter.validate_json(
        json.dumps({"kind": "transaction", "chain_id": 56, "tx_hash": HASH})
    )
    block = adapter.validate_json(
        json.dumps({"kind": "block", "chain_id": 56, "block_hash": HASH})
    )
    assert transaction.kind == "transaction" and block.kind == "block"
    for target in (
        {"kind": "transaction", "chain_id": 56, "block_hash": HASH},
        {"kind": "block", "chain_id": 56, "tx_hash": HASH},
        {"chain_id": 56, "tx_hash": HASH},
        {"kind": "transaction", "chain_id": 56, "tx_hash": ADDRESS},
    ):
        with pytest.raises(ValidationError):
            adapter.validate_json(json.dumps(target))


def test_draft_can_wait_for_intent_without_faking_execution_parameters():
    """仅贴哈希是有效输入；允许等待澄清，但不能标为 ready 后仍有意图缺口。"""
    draft = ResearchRequestDraft(
        user_input=HASH, readiness="needs_input", unresolved=("这是交易还是区块？",)
    )
    assert draft.goal is None and draft.targets == () and draft.time_selection is None
    with pytest.raises(ValidationError):
        ResearchRequestDraft(user_input=HASH, readiness="ready")


@pytest.mark.parametrize(
    ("amount", "unit", "hours"), [(24, "hour", 24), (2, "day", 48)]
)
def test_relative_time_uses_provided_collection_cutoff(amount, unit, hours):
    """相对时间严格锚定输入截止；不读取当前时间，也不会再次向前减三小时。"""
    cutoff = datetime(2026, 11, 8, 18, 17, tzinfo=timezone(timedelta(hours=8)))
    selection = RelativeTimeSelection(kind="relative", amount=amount, unit=unit)
    period = resolve_time_selection(selection, data_cutoff_time=cutoff)
    assert period.end == cutoff
    assert period.start == cutoff - timedelta(hours=hours)


def test_absolute_time_is_preserved_or_rejected_without_clipping():
    """历史范围保持原值；超过截止直接报需调整，不裁剪成其他研究时段。"""
    period = TimePeriod.model_validate_json(json.dumps(PERIOD))
    selection = AbsoluteTimeSelection(kind="absolute", period=period)
    assert resolve_time_selection(selection, data_cutoff_time=period.end) == period
    with pytest.raises(ValueError, match="超出"):
        resolve_time_selection(
            selection, data_cutoff_time=period.end - timedelta(hours=1)
        )
    with pytest.raises(ValueError, match="时区"):
        resolve_time_selection(selection, data_cutoff_time=datetime(2026, 11, 9))


@pytest.mark.parametrize(
    "period",
    [
        {"start": PERIOD["end"], "end": PERIOD["end"]},
        {"start": PERIOD["end"], "end": PERIOD["start"]},
        {"start": "2026-11-08T00:00:00", "end": PERIOD["end"]},
    ],
)
def test_invalid_time_ranges_fail(period):
    """拒绝缺失时区、空范围和反向范围，避免分析含义随机器环境改变。"""
    with pytest.raises(ValidationError):
        TimePeriod.model_validate_json(json.dumps(period))


@pytest.mark.parametrize("amount", [0, -1, True, 1.5, "24"])
def test_relative_duration_is_positive_integer(amount):
    """用户选择的时长只接受正整数，不能隐式转换或退回默认最近七天。"""
    with pytest.raises(ValidationError):
        RelativeTimeSelection.model_validate_json(
            json.dumps({"kind": "relative", "amount": amount, "unit": "hour"})
        )


def test_comparison_and_context_are_optional_but_require_reasons_when_used():
    """重点范围即可执行时段工具；增加背景范围时必须显式记录分析理由。"""
    payload = TOOL_CASES[2][1]
    request = AnalyzeTokenActivityInput.model_validate_json(json.dumps(payload))
    assert (
        request.periods.comparison_periods == ()
        and request.periods.context_periods == ()
    )
    invalid = {
        **payload,
        "periods": {"focus_period": PERIOD, "context_periods": [{"period": PERIOD}]},
    }
    with pytest.raises(ValidationError):
        AnalyzeTokenActivityInput.model_validate_json(json.dumps(invalid))


def test_large_amount_round_trip_remains_exact():
    """覆盖完整 uint256 和更大的累计值；JSON 往返后仍逐位相同，没有浮点参与。"""
    for amount in (str(2**256 - 1), str((2**256 - 1) * 1000), "-123", "0"):
        metric = AmountMetric(
            kind="token_amount",
            metric_id="test_net_amount",
            value_raw=amount,
            decimals=8,
        )
        restored = AmountMetric.model_validate_json(metric.model_dump_json())
        assert restored.value_raw == amount
    with pytest.raises(ValidationError):
        AmountMetric(kind="token_amount", metric_id="test", value_raw=1.1, decimals=8)


@pytest.mark.parametrize("amount", ["01", "-0", "1.2", "1e18", "+1", "NaN"])
def test_amounts_use_canonical_integer_strings(amount):
    """不接受科学计数法、非整数或含糊格式，确保后续精确计算有统一输入。"""
    with pytest.raises(ValidationError):
        AmountMetric(
            kind="token_amount", metric_id="test", value_raw=amount, decimals=None
        )


def test_unknown_and_zero_are_distinct():
    """未知精度保留 null，真实计数零保留 '0'，分母零不能伪造成有效比例。"""
    assert (
        AmountMetric(
            kind="token_amount", metric_id="test", value_raw="0", decimals=None
        ).decimals
        is None
    )
    assert CountMetric(kind="count", metric_id="test", value="0").value == "0"
    with pytest.raises(ValidationError):
        RatioMetric(kind="ratio", metric_id="test", numerator="0", denominator="0")


@pytest.mark.parametrize("status", ["success", "partial"])
def test_data_results_require_provenance_and_partial_requires_limitations(status):
    """成功和部分结果必须能追溯数据源；partial 还必须交代缺口。"""
    outcome = {"status": status, "data": {"events": []}}
    if status == "partial":
        outcome["limitations"] = ["只返回已索引范围"]
    payload = {"tool_id": "lookup_transaction_events", "call": CALL, "outcome": outcome}
    with pytest.raises(ValidationError):
        ToolResult[EventLookupData].model_validate_json(json.dumps(payload))
    payload["metadata"] = sample_metadata()
    result = ToolResult[EventLookupData].model_validate_json(json.dumps(payload))
    assert result.outcome.data.events == ()
    if status == "partial":
        outcome["limitations"] = []
        with pytest.raises(ValidationError):
            ToolResult[EventLookupData].model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "outcome",
    [
        {
            "status": "failed",
            "error": {
                "code": "dependency_unavailable",
                "public_message": "数据服务暂不可用",
            },
        },
        {"status": "needs_input", "questions": ["你希望研究哪段时间？"]},
        {"status": "unsupported", "reason": "当前未采集该范围"},
        {"status": "cancelled", "reason": "执行方已确认停止"},
    ],
)
def test_non_success_states_cannot_carry_fake_empty_data(outcome):
    """异常、追问、不支持与取消都保持原状态；不能附带空 data 伪装成功。"""
    payload = {"tool_id": "lookup_transaction_events", "call": CALL, "outcome": outcome}
    result = ToolResult[EventLookupData].model_validate_json(json.dumps(payload))
    assert result.outcome.status == outcome["status"]
    assert result.call.call_id == CALL["call_id"]
    outcome = {**outcome, "data": {"events": []}}
    with pytest.raises(ValidationError):
        ToolResult[EventLookupData].model_validate_json(
            json.dumps({**payload, "outcome": outcome})
        )


def test_analysis_finding_cannot_reference_unknown_result_version():
    """分析发现必须引用本次声明的结果版本；结构检查不把虚构样例变为真实发现。"""
    reference = {"result_id": "test-result", "request_revision": 1}
    payload = {
        "research_id": "test-research",
        "request_revision": 1,
        "result_refs": [reference],
        "findings": [
            {
                "finding_id": "f1",
                "statement": "仅用于测试的陈述",
                "result_refs": [reference],
            }
        ],
    }
    AnalysisResult.model_validate_json(json.dumps(payload))
    payload["findings"][0]["result_refs"][0] = {
        "result_id": "unknown",
        "request_revision": 2,
    }
    with pytest.raises(ValidationError, match="结果版本"):
        AnalysisResult.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("code", ["invalid_input", "permission_denied"])
def test_permission_and_invalid_input_are_never_retryable(code):
    """错误合同不能鼓励重复提交无权或非法调用，避免后续重试层语义冲突。"""
    with pytest.raises(ValidationError, match="不可标记"):
        SafeError(code=code, public_message="本地测试消息", retryable=True)


@pytest.mark.parametrize("roles", [("context",), ("focus", "focus")])
def test_activity_output_requires_one_focus_period(roles):
    """只有背景或多个不明重点的输出都不能冒充围绕用户所选时段的结果。"""
    slices = [
        {
            "role": role,
            "period": PERIOD,
            "metrics": [{"kind": "count", "metric_id": "test", "value": "0"}],
        }
        for role in roles
    ]
    with pytest.raises(ValidationError, match="focus"):
        TokenActivityData.model_validate_json(
            json.dumps({"chain_id": 56, "token_address": TOKEN, "slices": slices})
        )


@pytest.mark.parametrize("definition", TOOL_DEFINITIONS, ids=lambda item: item.tool_id)
def test_each_tool_accepts_its_typed_result_and_requires_call_identity(definition):
    """逐个验证四类输出 JSON 可解析，并确保所有返回保留调用关联，不是无归属数据。"""
    data = {"events": []}
    if definition.tool_id.startswith("analyze_"):
        data = {
            "chain_id": 56,
            "token_address": TOKEN,
            "slices": [
                {
                    "role": "focus",
                    "period": PERIOD,
                    "metrics": [{"kind": "count", "metric_id": "test", "value": "0"}],
                }
            ],
        }
        if definition.tool_id == "analyze_address_activity":
            data["address"] = ADDRESS
    payload = {
        "tool_id": definition.tool_id,
        "call": CALL,
        "outcome": {"status": "success", "data": data},
        "metadata": sample_metadata(),
    }
    result = definition.parse_result_json(json.dumps(payload))
    assert result.outcome.status == "success"
    del payload["call"]
    with pytest.raises(ValidationError):
        definition.parse_result_json(json.dumps(payload))
