"""Explicit, audited recovery of quarantined Transfers from transaction receipts."""

import json

import sqlalchemy as sa

from . import jobs as job_state
from .database import utcnow
from .errors import ChainScopeError
from .headers import header_row, save_headers
from .ingestion import checkpoint
from .models import ingestion, pending_transfers, window_diffs, window_events, windows
from .parser import HASH, fixed_hex, quantity
from .sqd import block_transfers
from .validator import TRANSFER_TOPIC, verify_receipt

PENDING_FIELDS = (
    "tx_hash",
    "log_index",
    "block_number",
    "from_address",
    "to_address",
    "amount_raw",
    "block_time_utc",
)


def receipt_transfers(rpc, token, tx_hash, first, last):
    """Read every BEM Transfer of one successful transaction, with its block."""
    receipt = rpc.call("eth_getTransactionReceipt", [tx_hash])
    if (
        not isinstance(receipt, dict)
        or fixed_hex(receipt.get("transactionHash"), HASH, "receipt.tx") != tx_hash
        or quantity(receipt.get("status"), "receipt.status") != 1
        or not isinstance(receipt.get("logs"), list)
    ):
        raise ChainScopeError("修复回执缺失、交易不符或交易未成功")
    number = quantity(receipt.get("blockNumber"), "receipt.block")
    if not first <= number <= last:
        raise ChainScopeError("修复交易移动到了窗口外，需要单独处理跨窗口差异")
    raw = rpc.call("eth_getBlockByNumber", [hex(number), False])
    if (
        not isinstance(raw, dict)
        or quantity(raw.get("number"), "block.number") != number
        or fixed_hex(raw.get("hash"), HASH, "block.hash")
        != fixed_hex(receipt.get("blockHash"), HASH, "receipt.block_hash")
    ):
        raise ChainScopeError("修复回执与实时区块头不一致")
    stamp = quantity(raw.get("timestamp"), "block.timestamp")
    logs = []
    for log in receipt["logs"]:
        if not isinstance(log, dict):
            raise ChainScopeError("修复回执日志格式错误")
        topics = log.get("topics") or []
        if (
            str(log.get("address", "")).lower() == token
            and isinstance(topics, list)
            and topics
            and str(topics[0]).lower() == TRANSFER_TOPIC
        ):
            logs.append(dict(log, logIndex=quantity(log.get("logIndex"), "log.index")))
    events = block_transfers(
        {
            "header": {"number": number, "hash": raw["hash"], "timestamp": stamp},
            "logs": logs,
        },
        token,
    )
    rows = []
    for event in events:
        row = {k: v for k, v in event.items() if k != "amount"}
        row["amount_raw"] = str(event["amount"])
        verify_receipt(row, receipt)
        rows.append(row)
    return rows, header_row(number, raw["hash"], stamp)


def recover_receipts(engine, job_id, start, tx_hashes, rpc):
    """Repair only an explicitly supplied transaction list; never admit directly.

    Original rows, replacements, count and checkpoint commit together. The
    ordinary admission checks still have to succeed before formal statistics.
    """
    hashes = sorted({fixed_hex(tx, HASH, "recovery.tx") for tx in tx_hashes})
    if not hashes or len(hashes) > 100:
        raise ChainScopeError("修复必须显式指定 1 到 100 笔异常交易")
    with job_state.job_lock(engine, job_id):
        with engine.connect() as c:
            job = job_state.load_job(c, job_id)
            window = (
                c.execute(
                    sa.select(windows).where(
                        windows.c.job_id == job_id,
                        windows.c.start_block == start,
                    )
                )
                .mappings()
                .one()
            )
            prior = (
                c.execute(
                    sa.select(window_diffs.c.tx_hash).where(
                        window_diffs.c.job_id == job_id,
                        window_diffs.c.start_block == start,
                        window_diffs.c.kind == "receipt_recovered",
                    )
                )
                .scalars()
                .all()
            )
            if window["status"] in ("pending", "done") and set(hashes) == set(prior):
                return {"start": start, "already_repaired": True}
            if (
                window["status"] != "failed"
                or window["error_type"] != "ReconcileMismatch"
            ):
                raise ChainScopeError("只修复已经调查确认的 ReconcileMismatch 隔离窗口")
            saved = checkpoint(c, job_id, start)
            if not saved or not saved["complete"] or saved["provider"] != "alchemy":
                raise ChainScopeError("修复要求已经完整保存的 Alchemy 窗口")
        replacements, headers = {}, []
        for tx in hashes:
            rows, header = receipt_transfers(
                rpc,
                job["token_address"],
                tx,
                start,
                window["end_block"],
            )
            replacements[tx] = rows
            headers.append(header)
        save_headers(engine, headers)
        condition = (pending_transfers.c.job_id == job_id) & (
            pending_transfers.c.start_block == start
        )
        with engine.begin() as c:
            current = (
                c.execute(
                    sa.select(windows)
                    .where(
                        windows.c.job_id == job_id,
                        windows.c.start_block == start,
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            locked = (
                c.execute(
                    sa.select(ingestion)
                    .where(
                        ingestion.c.job_id == job_id,
                        ingestion.c.start_block == start,
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if (
                current["status"] != "failed"
                or current["error_type"] != "ReconcileMismatch"
                or locked["generation"] != saved["generation"]
                or not locked["complete"]
            ):
                raise ChainScopeError("修复期间窗口或页进度发生变化，不写入")
            if c.scalar(
                sa.select(sa.func.count())
                .select_from(window_events)
                .where(
                    window_events.c.job_id == job_id,
                    window_events.c.start_block == start,
                )
            ):
                raise ChainScopeError("窗口已经存在正式事件关联，不覆盖正式数据")
            originals = [
                dict(r)
                for r in c.execute(
                    sa.select(pending_transfers)
                    .where(
                        condition,
                        pending_transfers.c.tx_hash.in_(hashes),
                    )
                    .with_for_update()
                ).mappings()
            ]
            active = [r for r in originals if r["generation"] == saved["generation"]]
            if set(r["tx_hash"] for r in active) != set(hashes):
                raise ChainScopeError("指定交易不在当前窗口的有效暂存数据中")
            count_before = c.scalar(
                sa.select(sa.func.count())
                .select_from(pending_transfers)
                .where(
                    condition,
                    pending_transfers.c.generation == saved["generation"],
                )
            )
            if count_before != locked["event_count"]:
                raise ChainScopeError("修复前暂存行数与页进度不一致")
            replacement_rows = [r for rows in replacements.values() for r in rows]
            count_after = count_before - len(active) + len(replacement_rows)
            if count_after > 50_000:
                raise ChainScopeError("修复后窗口超过当前事件上限，需要拆分")
            now = utcnow()
            for tx in hashes:
                detail = {
                    "source": "alchemy_transaction_receipt_and_block_header",
                    "original_rows": [r for r in originals if r["tx_hash"] == tx],
                    "replacement_rows": replacements[tx],
                    "generation": saved["generation"],
                }
                c.execute(
                    window_diffs.insert().values(
                        job_id=job_id,
                        start_block=start,
                        end_block=window["end_block"],
                        found_at_utc=now,
                        kind="receipt_recovered",
                        tx_hash=tx,
                        detail=json.loads(json.dumps(detail, default=str)),
                    )
                )
            c.execute(
                pending_transfers.delete().where(
                    condition,
                    pending_transfers.c.tx_hash.in_(hashes),
                )
            )
            if replacement_rows:
                c.execute(
                    pending_transfers.insert(),
                    [
                        {
                            "job_id": job_id,
                            "start_block": start,
                            "generation": saved["generation"],
                            **{k: row[k] for k in PENDING_FIELDS},
                        }
                        for row in replacement_rows
                    ],
                )
            actual = c.scalar(
                sa.select(sa.func.count())
                .select_from(pending_transfers)
                .where(
                    condition,
                    pending_transfers.c.generation == saved["generation"],
                )
            )
            if actual != count_after:
                raise ChainScopeError("修复后行数读回不一致")
            c.execute(
                ingestion.update()
                .where(
                    ingestion.c.job_id == job_id,
                    ingestion.c.start_block == start,
                )
                .values(event_count=count_after, updated_at_utc=now)
            )
            c.execute(
                windows.update()
                .where(
                    windows.c.job_id == job_id,
                    windows.c.start_block == start,
                )
                .values(status="pending", error_type=None, updated_at_utc=now)
            )
            job_state.fault("before_receipt_repair_commit")
        return {
            "start": start,
            "transactions": len(hashes),
            "original_events": len(active),
            "replacement_events": len(replacement_rows),
            "window_events_before": count_before,
            "window_events_after": count_after,
        }
