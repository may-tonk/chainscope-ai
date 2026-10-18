"""Detect incompatible block assignments without treating multi-log txs as duplicates."""

import time
from collections import defaultdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import sqlalchemy as sa

from .errors import ChainScopeError, ReconcileMismatch
from .models import transfers, window_events
from .parser import HASH, fixed_hex, quantity
from .validator import TRANSFER_TOPIC, verify_receipt

CHUNK = 1000
MAX_GUARD_RECEIPTS = 10


def block_conflicts(rows, other_rows=()):
    """Multiple logs in ONE block are legal; multiple block numbers are not."""
    blocks = defaultdict(set)
    for row in rows:
        blocks[row["tx_hash"]].add(row["block_number"])
    for row in other_rows:
        if row["tx_hash"] in blocks:
            blocks[row["tx_hash"]].add(row["block_number"])
    return [
        {
            "kind": "transaction_block_conflict",
            "tx_hash": tx,
            "detail": {"block_numbers": sorted(numbers)},
        }
        for tx, numbers in sorted(blocks.items())
        if len(numbers) > 1
    ]


def transaction_conflicts(connection, chain_id, rows, job_id=None):
    """Check active formal links, excluding historical rows dropped by a redo."""
    conflicts = block_conflicts(rows)
    if conflicts:
        return conflicts
    hashes = sorted({r["tx_hash"] for r in rows})
    existing = []
    active = sa.select(1).where(window_events.c.event_id == transfers.c.event_id)
    if job_id is not None:
        active = active.where(window_events.c.job_id == job_id)
    for offset in range(0, len(hashes), CHUNK):
        existing.extend(
            connection.execute(
                sa.select(transfers.c.tx_hash, transfers.c.block_number).where(
                    transfers.c.chain_id == chain_id,
                    transfers.c.tx_hash.in_(hashes[offset : offset + CHUNK]),
                    sa.exists(active),
                )
            ).mappings()
        )
    return block_conflicts(rows, existing)


def reject_conflicting_transactions(
    connection, chain_id, rows, rpc, stop=lambda: False, job_id=None
):
    """Only suspect transactions need extra RPCs. Never repair during admission."""
    conflicts = transaction_conflicts(connection, chain_id, rows, job_id)
    if not conflicts:
        return
    evidence = conflicts[:1000]
    for diff in evidence[:MAX_GUARD_RECEIPTS]:
        if stop():
            break
        try:
            receipt = rpc.call("eth_getTransactionReceipt", [diff["tx_hash"]])
            if (
                not isinstance(receipt, dict)
                or fixed_hex(receipt.get("transactionHash"), HASH, "receipt.tx")
                != diff["tx_hash"]
            ):
                raise ChainScopeError("冲突交易回执缺失或交易哈希不符")
            diff["detail"]["receipt_block_number"] = quantity(
                receipt.get("blockNumber"), "receipt.block"
            )
            diff["detail"]["receipt_block_hash"] = fixed_hex(
                receipt.get("blockHash"), HASH, "receipt.block_hash"
            )
        except (ChainScopeError, httpx.HTTPError) as exc:
            # The contradiction is already proven locally; an unavailable
            # receipt must not turn it into a normal retry or source fallback.
            diff["detail"]["receipt_error"] = type(exc).__name__
            break
    raise ReconcileMismatch(
        f"{len(conflicts)} 笔交易对应多个区块，已阻止入库；"
        f"最多查询 {MAX_GUARD_RECEIPTS} 笔冲突回执，不自动修复",
        evidence,
    )


# EXPLAIN on the project's MySQL confirms ordered uq_transfer_identity access
# and an indexed window_events lookup, with no filesort. Stream one tx at a
# time, so cross-window conflicts do not require millions of Python dict keys.
SCAN_SQL = """
SELECT STRAIGHT_JOIN t.tx_hash, t.block_number, w.start_block
FROM transfer_events t FORCE INDEX (uq_transfer_identity)
JOIN window_events w ON w.event_id=t.event_id AND w.job_id=:job
WHERE t.chain_id=:chain AND t.token_address=:token
  AND t.block_number BETWEEN :first AND :last
ORDER BY t.tx_hash, t.log_index
"""


def scan_candidates(connection, job, cutoff, notify=lambda _: None):
    """Read only identity/coverage metadata; each tx is contiguous in the index."""
    candidates, scanned, tx, blocks, ranges = [], 0, None, defaultdict(int), set()
    last_notice = time.monotonic()

    def finish():
        if len(blocks) > 1:
            candidates.append(
                {
                    "tx_hash": tx,
                    "blocks": [
                        {"block_number": n, "events": count}
                        for n, count in sorted(blocks.items())
                    ],
                    "window_starts": sorted(ranges),
                }
            )
            if len(candidates) > 1000:
                raise ChainScopeError("跨区块交易超过1000笔；停止，需另行确定调查范围")

    params = {
        "job": job["job_id"],
        "chain": job["chain_id"],
        "token": job["token_address"],
        "first": job["start_block"],
        "last": cutoff,
    }
    with connection.execute(
        sa.text(SCAN_SQL).execution_options(stream_results=True, yield_per=5000), params
    ) as result:
        for row in result:
            if row.tx_hash != tx:
                finish()
                tx, blocks, ranges = row.tx_hash, defaultdict(int), set()
            blocks[row.block_number] += 1
            ranges.add(row.start_block)
            scanned += 1
            if scanned % 250_000 == 0 and time.monotonic() - last_notice >= 10:
                notify(
                    {
                        "phase": "local_transaction_scan",
                        "events_scanned": scanned,
                        "candidates_found": len(candidates),
                    }
                )
                last_notice = time.monotonic()
    finish()
    return scanned, candidates


def _verify_candidate(connection, job, rpc, candidate):
    from .acceptance import canonical_events, compare_events

    tx = candidate["tx_hash"]
    formal = [
        dict(r)
        for r in connection.execute(
            sa.select(transfers)
            .join(window_events, window_events.c.event_id == transfers.c.event_id)
            .where(
                window_events.c.job_id == job["job_id"],
                transfers.c.chain_id == job["chain_id"],
                transfers.c.tx_hash == tx,
                transfers.c.token_address == job["token_address"],
            )
        ).mappings()
    ]
    receipt = rpc.call("eth_getTransactionReceipt", [tx])
    if (
        not isinstance(receipt, dict)
        or fixed_hex(receipt.get("transactionHash"), HASH, "receipt.tx") != tx
        or quantity(receipt.get("status"), "receipt.status") != 1
        or not isinstance(receipt.get("logs"), list)
    ):
        raise ChainScopeError("冲突交易回执缺失、哈希不符或执行未成功")
    number = quantity(receipt.get("blockNumber"), "receipt.block")
    header = rpc.call("eth_getBlockByNumber", [hex(number), False])
    if (
        not isinstance(header, dict)
        or quantity(header.get("number"), "header.number") != number
        or fixed_hex(header.get("hash"), HASH, "header.hash")
        != fixed_hex(receipt.get("blockHash"), HASH, "receipt.block_hash")
    ):
        raise ChainScopeError("冲突交易回执与实时区块头不一致")
    logs = []
    for log in receipt["logs"]:
        if not isinstance(log, dict):
            raise ChainScopeError("回执日志格式错误")
        topics = log.get("topics") or []
        if (
            str(log.get("address", "")).lower() == job["token_address"]
            and isinstance(topics, list)
            and topics
            and str(topics[0]).lower() == TRANSFER_TOPIC
        ):
            logs.append(log)
    canonical = canonical_events(logs, header, job["token_address"])
    for event in canonical:
        verify_receipt(
            {**event, "token_address": job["token_address"], "tx_hash": tx}, receipt
        )
    comparison = compare_events(formal, canonical)
    adjustments = defaultdict(int)
    for sign, rows in ((-1, formal), (1, canonical)):
        for row in rows:
            amount = sign * int(row["amount_raw"])
            adjustments[row["from_address"]] -= amount
            adjustments[row["to_address"]] += amount
    return {
        **candidate,
        "status": "verified",
        "receipt_block_number": number,
        "comparison": comparison,
        "formal_rows": formal,
        "canonical_rows": canonical,
        "proposed_balance_deltas_raw": {
            a: str(v) for a, v in sorted(adjustments.items()) if v
        },
        "proposed_event_count_delta": len(canonical) - len(formal),
    }


def scan_transaction_blocks(
    engine, rpc, job_id, cutoff, *, max_receipts=50, notify=lambda _: None
):
    """Read-only same-fault screening. Proposed changes are never applied."""
    from .jobs import load_job

    if cutoff < 0 or not 0 <= max_receipts <= 100:
        raise ChainScopeError("截止区块无效或回执上限不在0到100之间")
    started = time.monotonic()
    report = {
        "job_id": job_id,
        "cutoff_block": cutoff,
        "read_only": True,
        "rule": "同一交易哈希在正式任务关联中对应多个区块号",
        "started_at_beijing": datetime.now(UTC)
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .isoformat(),
        "max_receipts": max_receipts,
    }
    with engine.connect() as c:
        c.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        job = load_job(c, job_id)
        if job["chain_id"] != 56 or job["verified_through"] != cutoff:
            raise ChainScopeError("只读筛查要求BSC任务当前连续进度等于指定截止区块")
        report.update(
            start_block=job["start_block"], token_address=job["token_address"]
        )
        scanned, candidates = scan_candidates(c, job, cutoff, notify)
        report.update(
            events_scanned=scanned,
            local_scan_complete=True,
            candidate_count=len(candidates),
            candidates=[],
        )
        source_available = True
        if candidates and max_receipts:
            if quantity(rpc.call("eth_chainId", []), "chain_id") != job["chain_id"]:
                raise ChainScopeError("RPC链ID与任务不一致")
        for index, candidate in enumerate(candidates):
            if index >= max_receipts or not source_available:
                report["candidates"].append({**candidate, "status": "not_checked"})
                continue
            try:
                checked = _verify_candidate(c, job, rpc, candidate)
            except (ChainScopeError, httpx.HTTPError) as exc:
                source_available = False
                checked = {
                    **candidate,
                    "status": "inconclusive",
                    "error": str(exc)
                    if isinstance(exc, ChainScopeError)
                    else type(exc).__name__,
                }
            report["candidates"].append(checked)
            notify(
                {
                    "phase": "suspect_receipt_check",
                    "checked": index + 1,
                    "candidates": len(candidates),
                }
            )
    checked = [r for r in report["candidates"] if r["status"] == "verified"]
    deltas = defaultdict(int)
    for row in checked:
        for address, amount in row["proposed_balance_deltas_raw"].items():
            deltas[address] += int(amount)
    report.update(
        ok=not candidates,
        verification_complete=len(checked) == len(candidates),
        verified_candidates=len(checked),
        proposed_event_count_delta=sum(
            r["proposed_event_count_delta"] for r in checked
        ),
        proposed_balance_deltas_raw={a: str(v) for a, v in sorted(deltas.items()) if v},
        elapsed_seconds=round(time.monotonic() - started, 2),
        rpc_requests=rpc.requests,
        rpc_calls=dict(rpc.calls),
        limitations=[
            "仅筛查同一交易跨区块的已知错误模式，不证明全历史没有其他类型错误",
            "未修改正式记录、窗口状态、计数、进度或余额；所有修复数值仅为建议",
            "回执核对仅对命中交易执行，来自现有Alchemy节点",
        ],
    )
    return report
