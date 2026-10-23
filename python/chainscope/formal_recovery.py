"""Explicit removal of receipt-disproved formal rows, with atomic evidence."""

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa

from . import jobs as job_state
from .acceptance import compare_events
from .database import utcnow
from .errors import ChainScopeError
from .holders import BALANCE_OF
from .models import (
    balances,
    batch_events,
    ingestion,
    pending_transfers,
    swaps,
    transfers,
    window_diffs,
    window_events,
    windows,
)
from .parser import HASH, fixed_hex, quantity
from .recovery import PENDING_FIELDS, receipt_transfers
from .validator import Tally

KIND = "formal_receipt_repaired"


def _json(value):
    return json.loads(
        json.dumps(value, default=lambda v: v.hex() if isinstance(v, bytes) else str(v))
    )


def _verify_backup(engine, backup):
    if (
        not backup.get("completed")
        or backup.get("schema_revision") != "0008"
        or backup.get("database") != engine.url.database
    ):
        raise ChainScopeError("缺少本数据库的完整备份证明")
    path = Path(backup["path"])
    if not path.is_file() or path.stat().st_size != backup["bytes"]:
        raise ChainScopeError("备份文件不存在或大小已变化")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != backup["sha256"]:
        raise ChainScopeError("备份SHA256不符，不执行修复")


def _rows(c, job_id, tx, chain):
    return [
        dict(r)
        for r in c.execute(
            sa.select(transfers)
            .join(window_events, window_events.c.event_id == transfers.c.event_id)
            .where(
                window_events.c.job_id == job_id,
                transfers.c.chain_id == chain,
                transfers.c.tx_hash == tx,
            )
            .with_for_update()
        ).mappings()
    ]


def _exact_rows(actual, expected):
    return sorted((_json(r) for r in actual), key=lambda r: r["event_id"]) == sorted(
        (_json(r) for r in expected), key=lambda r: r["event_id"]
    )


def repair_completed_receipt(
    engine,
    rpc,
    *,
    job_id,
    start,
    cutoff,
    tx_hash,
    expected_rows,
    expected_deltas,
    expected_window_count,
    expected_job_total,
    backup,
):
    """Only remove approved extras; never insert, reset windows or rescan history.

    The full backup, exact pre-image, receipt, fixed-block balances, window
    fingerprint and staging count must agree. A mismatch aborts the transaction.
    """
    _verify_backup(engine, backup)
    tx = fixed_hex(tx_hash, HASH, "repair.tx")
    expected_rows = [dict(r) for r in expected_rows]
    for row in expected_rows:
        if isinstance(row.get("block_time_utc"), str):
            row["block_time_utc"] = datetime.fromisoformat(row["block_time_utc"])
        if row["block_time_utc"].tzinfo is not None:
            row["block_time_utc"] = (
                row["block_time_utc"].astimezone(UTC).replace(tzinfo=None)
            )
    if not expected_rows or any(r["tx_hash"] != tx for r in expected_rows):
        raise ChainScopeError("修复清单必须明确指定同一笔交易的原始正式记录")
    with job_state.job_lock(engine, job_id):
        if quantity(rpc.call("eth_chainId", []), "chain_id") != 56:
            raise ChainScopeError("修复节点不是BSC主网")
        with engine.connect() as c:
            job = job_state.load_job(c, job_id)
            end = c.scalar(
                sa.select(windows.c.end_block).where(
                    windows.c.job_id == job_id, windows.c.start_block == start
                )
            )
        canonical, _ = receipt_transfers(rpc, job["token_address"], tx, start, end)
        comparison = compare_events(expected_rows, canonical)
        if (
            comparison["changed"]
            or comparison["missing_in_database"]
            or not comparison["extra_in_database"]
        ):
            raise ChainScopeError("回执不再对应仅移除多余记录的方案，停止修复")
        keep = {(r["tx_hash"], r["log_index"]) for r in canonical}
        removed = [
            r for r in expected_rows if (r["tx_hash"], r["log_index"]) not in keep
        ]
        delta = defaultdict(int)
        for row in removed:
            delta[row["from_address"]] += int(row["amount_raw"])
            delta[row["to_address"]] -= int(row["amount_raw"])
        delta = {a: v for a, v in delta.items() if v}
        if delta != {a: int(v) for a, v in expected_deltas.items()}:
            raise ChainScopeError("余额调整与明确批准的清单不一致")
        chain_balances = {
            a: quantity(
                rpc.call(
                    "eth_call",
                    [
                        {
                            "to": job["token_address"],
                            "data": BALANCE_OF + "0" * 24 + a[2:],
                        },
                        hex(cutoff),
                    ],
                ),
                "balanceOf",
            )
            for a in sorted(delta)
        }
        with engine.begin() as c:
            job = job_state.load_job(c, job_id, lock=True)
            if job["verified_through"] != cutoff or job["balances_through"] != cutoff:
                raise ChainScopeError("任务或持仓进度变化，停止修复")
            window = dict(
                c.execute(
                    sa.select(windows)
                    .where(windows.c.job_id == job_id, windows.c.start_block == start)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            saved = dict(
                c.execute(
                    sa.select(ingestion)
                    .where(
                        ingestion.c.job_id == job_id, ingestion.c.start_block == start
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if (
                window["status"] != "done"
                or not saved["complete"]
                or saved["provider"] != "alchemy"
            ):
                raise ChainScopeError("只修复已有完整Alchemy暂存的已完成窗口")
            actual = _rows(c, job_id, tx, job["chain_id"])
            prior = (
                c.execute(
                    sa.select(window_diffs).where(
                        window_diffs.c.job_id == job_id,
                        window_diffs.c.start_block == start,
                        window_diffs.c.tx_hash == tx,
                        window_diffs.c.kind == KIND,
                    )
                )
                .mappings()
                .first()
            )
            cache = {
                r["address"]: dict(r)
                for r in c.execute(
                    sa.select(balances)
                    .where(
                        balances.c.chain_id == job["chain_id"],
                        balances.c.token_address == job["token_address"],
                        balances.c.address.in_(sorted(delta)),
                    )
                    .order_by(balances.c.address)
                    .with_for_update()
                ).mappings()
            }
            before_tally = job_state._window_tally(
                c, job_id, start, window["fingerprint_version"] or 1
            )
            if not before_tally.matches(
                window["event_count"], int(window["amount_sum"]), window["fingerprint"]
            ):
                raise ChainScopeError("修复前窗口数量、金额或指纹不一致")
            if prior:
                if (
                    prior["detail"]["removed_event_ids"]
                    != sorted(r["event_id"] for r in removed)
                    or not compare_events(actual, canonical)["ok"]
                    or any(
                        a not in cache
                        or int(cache[a]["balance_raw"]) != chain_balances[a]
                        for a in delta
                    )
                ):
                    raise ChainScopeError("已有修复证据与当前状态不一致")
                return {
                    **prior["detail"]["result"],
                    "audit_id": prior["diff_id"],
                    "already_repaired": True,
                }
            if not _exact_rows(actual, expected_rows):
                raise ChainScopeError("正式记录与已批准的原始清单不同，停止修复")
            total = int(
                c.scalar(
                    sa.select(sa.func.sum(windows.c.event_count)).where(
                        windows.c.job_id == job_id, windows.c.status == "done"
                    )
                )
            )
            if (
                total != expected_job_total
                or window["event_count"] != expected_window_count
                or window["inserted_count"] != expected_window_count
            ):
                raise ChainScopeError("总量、窗口计数或插入计数变化，停止修复")
            ids = sorted(r["event_id"] for r in removed)
            links = [
                dict(r)
                for r in c.execute(
                    sa.select(window_events)
                    .where(window_events.c.event_id.in_(ids))
                    .with_for_update()
                ).mappings()
            ]
            if len(links) != len(ids) or any(
                r["job_id"] != job_id or r["start_block"] != start for r in links
            ):
                raise ChainScopeError("多余记录还关联其他窗口或任务，不能按本方案移除")
            if c.scalar(
                sa.select(sa.func.count())
                .select_from(batch_events)
                .where(batch_events.c.event_id.in_(ids))
            ):
                raise ChainScopeError("存在额外批次关联，需单独确定修复范围")
            original_pending = [
                dict(r)
                for r in c.execute(
                    sa.select(pending_transfers)
                    .where(
                        pending_transfers.c.job_id == job_id,
                        pending_transfers.c.start_block == start,
                        pending_transfers.c.tx_hash == tx,
                    )
                    .with_for_update()
                ).mappings()
            ]
            active = [
                r for r in original_pending if r["generation"] == saved["generation"]
            ]
            staged_count = c.scalar(
                sa.select(sa.func.count())
                .select_from(pending_transfers)
                .where(
                    pending_transfers.c.job_id == job_id,
                    pending_transfers.c.start_block == start,
                    pending_transfers.c.generation == saved["generation"],
                )
            )
            if (
                staged_count != saved["event_count"]
                or staged_count != expected_window_count
                or {tuple(str(r[k]) for k in PENDING_FIELDS) for r in active}
                != {tuple(str(r[k]) for k in PENDING_FIELDS) for r in expected_rows}
            ):
                raise ChainScopeError("暂存内容或页计数与正式清单不同")
            if any(
                a not in cache or int(cache[a]["balance_raw"]) + v != chain_balances[a]
                for a, v in delta.items()
            ):
                raise ChainScopeError("批准的余额调整不能与固定区块链上余额吻合")
            swap_before = [
                dict(r)
                for r in c.execute(
                    sa.select(swaps).where(
                        swaps.c.job_id == job_id, swaps.c.tx_hash == tx
                    )
                ).mappings()
            ]
            if c.execute(
                window_events.delete().where(
                    window_events.c.job_id == job_id,
                    window_events.c.start_block == start,
                    window_events.c.event_id.in_(ids),
                )
            ).rowcount != len(ids):
                raise ChainScopeError("移除正式关联数量不符")
            if c.execute(
                transfers.delete().where(transfers.c.event_id.in_(ids))
            ).rowcount != len(ids):
                raise ChainScopeError("移除多余记录数量不符")
            if c.execute(
                pending_transfers.delete().where(
                    pending_transfers.c.job_id == job_id,
                    pending_transfers.c.start_block == start,
                    pending_transfers.c.tx_hash == tx,
                    pending_transfers.c.log_index.in_(
                        [r["log_index"] for r in removed]
                    ),
                )
            ).rowcount != len(ids):
                raise ChainScopeError("移除多余暂存记录数量不符")
            job_state.fault("after_formal_repair_deletes")
            after_tally = job_state._window_tally(
                c, job_id, start, before_tally.version
            )
            removed_tally = Tally(before_tally.version)
            for r in removed:
                removed_tally.add(
                    tx,
                    r["log_index"],
                    r["from_address"],
                    r["to_address"],
                    int(r["amount_raw"]),
                    r["block_number"],
                )
            if (
                after_tally.events != before_tally.events - len(ids)
                or after_tally.amount != before_tally.amount - removed_tally.amount
                or after_tally.fingerprint
                != (before_tally.fingerprint - removed_tally.fingerprint) % 2**256
            ):
                raise ChainScopeError("修复后窗口读回不符，全部回滚")
            now = utcnow()
            c.execute(
                windows.update()
                .where(windows.c.job_id == job_id, windows.c.start_block == start)
                .values(
                    event_count=after_tally.events,
                    inserted_count=window["inserted_count"] - len(ids),
                    amount_sum=str(after_tally.amount),
                    fingerprint=after_tally.fingerprint_bytes(),
                    updated_at_utc=now,
                )
            )
            c.execute(
                ingestion.update()
                .where(ingestion.c.job_id == job_id, ingestion.c.start_block == start)
                .values(event_count=saved["event_count"] - len(ids), updated_at_utc=now)
            )
            for address, value in delta.items():
                c.execute(
                    balances.update()
                    .where(
                        balances.c.chain_id == job["chain_id"],
                        balances.c.token_address == job["token_address"],
                        balances.c.address == address,
                    )
                    .values(balance_raw=str(int(cache[address]["balance_raw"]) + value))
                )
            actual_after = _rows(c, job_id, tx, job["chain_id"])
            if not compare_events(actual_after, canonical)["ok"]:
                raise ChainScopeError("修复后交易记录不符合回执")
            cache_after = {
                r["address"]: str(int(r["balance_raw"]))
                for r in c.execute(
                    sa.select(balances).where(
                        balances.c.chain_id == job["chain_id"],
                        balances.c.token_address == job["token_address"],
                        balances.c.address.in_(sorted(delta)),
                    )
                ).mappings()
            }
            if cache_after != {a: str(v) for a, v in chain_balances.items()}:
                raise ChainScopeError("修复后余额读回不一致")
            swap_after = [
                dict(r)
                for r in c.execute(
                    sa.select(swaps).where(
                        swaps.c.job_id == job_id, swaps.c.tx_hash == tx
                    )
                ).mappings()
            ]
            if _json(swap_after) != _json(swap_before):
                raise ChainScopeError("买卖记录发生意外变化")
            result = dict(
                job_id=job_id,
                tx_hash=tx,
                removed_event_ids=ids,
                removed_records=len(ids),
                window_start=start,
                window_events_before=before_tally.events,
                window_events_after=after_tally.events,
                formal_events_before=total,
                formal_events_after=total - len(ids),
                balances_after=cache_after,
                balance_deltas_raw={a: str(v) for a, v in delta.items()},
                cutoff_block=cutoff,
                swaps_preserved=len(swap_after),
                already_repaired=False,
            )
            detail = _json(
                dict(
                    backup=backup,
                    original_formal_rows=actual,
                    original_pending_rows=original_pending,
                    original_window=window,
                    original_ingestion=saved,
                    original_balances=cache,
                    original_links=links,
                    removed_event_ids=ids,
                    canonical_rows=canonical,
                    swap_rows=swap_before,
                    result=result,
                    note="历史执行批次保留原始运行计数；正式窗口、暂存进度及余额按回执修复",
                )
            )
            audit = c.execute(
                window_diffs.insert().values(
                    job_id=job_id,
                    start_block=start,
                    end_block=end,
                    found_at_utc=now,
                    kind=KIND,
                    tx_hash=tx,
                    detail=detail,
                )
            )
            result["audit_id"] = audit.inserted_primary_key[0]
            job_state.fault("before_formal_repair_commit")
        return result
