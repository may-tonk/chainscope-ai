"""有界精确 Transfer 活动计算、地址贡献与比较；不生成价格或动机推断。"""

from collections import Counter

from chainscope_research.analysis.cleaning.transfer_rules import ZERO, classify_transfer
from chainscope_research.contracts.tools import (
    ActivitySlice,
    AddressContribution,
    AmountMetric,
    CountMetric,
    EventReference,
    MetricChange,
)


def event_reference(row: dict, chain_id: int, kind: str = "transfer") -> EventReference:
    """构造可回查定位；Swap 区块哈希缺失时保留 null，不编造值。"""
    return EventReference.model_validate(
        {
            "chain_id": chain_id,
            "tx_hash": row["tx_hash"],
            "log_index": row["log_index"],
            "block_number": str(row["block_number"]),
            "block_hash": row.get("block_hash"),
            "event_kind": kind,
        }
    )


def calculate_slice(
    rows: list[dict], source, period, role: str, address: str | None, settings, check
) -> ActivitySlice:
    """对当前筛选范围精确计算；同交易多日志只计一笔交易，贡献排名只用普通转账。

    总转账额包含分类前所有事件；普通转账额排除铸造、销毁与自转账。地址净流
    指普通转账流入减流出，不等于持仓余额或利润。样例只是头 N 条事件引用。
    """
    counts: Counter[str] = Counter()
    amounts: Counter[str] = Counter()
    transactions, senders, receivers = set(), set(), set()
    contributions: dict[tuple[str, str], int] = {}
    contribution_counts: Counter[tuple[str, str]] = Counter()
    evidence: list[EventReference] = []
    pairs: tuple[tuple[str, str], ...]
    for index, row in enumerate(rows):
        if index % 256 == 0:
            check()
        kind, amount = classify_transfer(row, source.job["token_decimals"])
        sender, receiver = row["from_address"].lower(), row["to_address"].lower()
        counts["transfer_event_count"] += 1
        counts[kind + "_event_count"] += 1
        counts["zero_amount_event_count"] += amount == 0
        amounts["transfer_amount_raw"] += amount
        amounts[kind + "_amount_raw"] += amount
        transactions.add(row["tx_hash"])
        if sender != ZERO:
            senders.add(sender)
        if receiver != ZERO:
            receivers.add(receiver)
        if len(evidence) < settings.evidence_limit:
            evidence.append(event_reference(row, source.scope.chain_id))
        if kind != "ordinary":
            continue
        if address:
            direction, counterparty = (
                ("inbound_counterparty", sender)
                if receiver == address
                else ("outbound_counterparty", receiver)
            )
            flow = "inflow_raw" if receiver == address else "outflow_raw"
            amounts[flow] += amount
            pairs = ((direction, counterparty),)
        else:
            pairs = (("sender", sender), ("receiver", receiver))
        for key in pairs:
            contributions[key] = contributions.get(key, 0) + amount
            contribution_counts[key] += 1
    counts.update(
        transaction_count=len(transactions),
        active_sender_count=len(senders),
        active_receiver_count=len(receivers),
        active_address_count=len(senders | receivers),
    )
    for kind in ("ordinary", "mint", "burn", "self", "unknown_zero"):
        counts.setdefault(kind + "_event_count", 0)
        amounts.setdefault(kind + "_amount_raw", 0)
    counts.setdefault("transfer_event_count", 0)
    counts.setdefault("zero_amount_event_count", 0)
    amounts.setdefault("transfer_amount_raw", 0)
    if address:
        amounts.setdefault("inflow_raw", 0)
        amounts.setdefault("outflow_raw", 0)
        amounts["net_transfer_flow_raw"] = (
            amounts["inflow_raw"] - amounts["outflow_raw"]
        )
    metrics: list[CountMetric | AmountMetric] = [
        CountMetric(kind="count", metric_id=key, value=str(value))
        for key, value in sorted(counts.items())
    ]
    metrics += [
        AmountMetric(
            kind="token_amount",
            metric_id=key,
            value_raw=str(value),
            decimals=source.job["token_decimals"],
        )
        for key, value in sorted(amounts.items())
    ]
    top: list[AddressContribution] = []
    for direction in sorted({key[0] for key in contributions}):
        ordered = sorted(
            (
                (key, value)
                for key, value in contributions.items()
                if key[0] == direction
            ),
            key=lambda item: (-item[1], item[0][1]),
        )[: settings.top_addresses]
        top.extend(
            AddressContribution.model_validate(
                {
                    "address": key[1],
                    "direction": direction,
                    "amount_raw": str(value),
                    "event_count": str(contribution_counts[key]),
                }
            )
            for key, value in ordered
        )
    return ActivitySlice.model_validate(
        {
            "role": role,
            "period": period,
            "metrics": tuple(metrics),
            "evidence": tuple(evidence),
            "evidence_is_sampled": len(rows) > len(evidence),
            "top_addresses": tuple(top),
        }
    )


def compare_slices(slices: list[ActivitySlice]) -> tuple[MetricChange, ...]:
    """输出精确差值；仅等长范围、正基准给相对变化，零基准不制造无穷增长率。"""
    focus = slices[0]

    def values(item):
        """提取计数和金额最小单位，避免百分数或显示值重新参加计算。"""
        return {
            metric.metric_id: int(
                metric.value if isinstance(metric, CountMetric) else metric.value_raw
            )
            for metric in item.metrics
            if isinstance(metric, (CountMetric, AmountMetric))
        }

    current = values(focus)
    changes = []
    for comparison in slices[1:]:
        if comparison.role != "comparison":
            continue
        equal = (
            focus.period.end - focus.period.start
            == comparison.period.end - comparison.period.start
        )
        for key, baseline in values(comparison).items():
            delta = current[key] - baseline
            relative = equal and baseline > 0 and key != "net_transfer_flow_raw"
            note = (
                "等长区间的精确变化；不是因果结论。"
                if equal
                else "区间长度不同，仅陈述总量差，不提供可比增长率。"
            )
            changes.append(
                MetricChange(
                    comparison_period=comparison.period,
                    metric_id=key,
                    focus_value=str(current[key]),
                    comparison_value=str(baseline),
                    delta=str(delta),
                    relative_numerator=str(delta) if relative else None,
                    relative_denominator=str(baseline) if relative else None,
                    note=note,
                )
            )
    return tuple(changes)
