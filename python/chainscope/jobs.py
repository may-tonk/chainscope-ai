"""Scan jobs and coverage windows: durable progress for resumable collection.

A job is one token on one source. Its block range is cut into windows; a window
is collected and committed as a unit, so it is also the resume point. The job's
`verified_through` only advances over an unbroken run of done windows.

A window becomes done only in the transaction that replaces its event links and
swaps and reads them back; if any check fails nothing of the attempt remains.
Windows whose data disagrees with stored data or a second source are
quarantined: failed, kept out of the watermark and skipped until `redo`.
"""

import hashlib
import os
import random
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from . import balances
from .database import utcnow
from .errors import ChainScopeError, EventConflict, ReadbackMismatch
from .models import (
    batches,
    jobs,
    swaps,
    transfers,
    window_diffs,
    window_events,
    windows,
)
from .validator import FINGERPRINT_VERSION, Tally

CHUNK = 1000
# MySQL errors after which the whole window transaction can safely run again.
RETRYABLE = {1205, 1213}  # lock wait timeout, deadlock
LOST = {2006, 2013}  # server gone away, lost connection
FAULT_EXIT = 75
# Failed windows with these errors wait for a person (redo) instead of a retry.
QUARANTINE = (
    "EventConflict",
    "ReadbackMismatch",
    "ReconcileMismatch",
    "RecheckChanged",
    "BlockTooLarge",
)
# Above this many blocks a balance rewind rebuilds instead of subtracting.
BALANCE_REWIND_LIMIT = 200_000
# Transfer fields compared when the same event is fetched again.
EVENT_FIELDS = (
    "token_address",
    "block_number",
    "block_hash",
    "block_time_utc",
    "from_address",
    "to_address",
    "amount_raw",
)


def fault(point: str) -> None:
    """Crash on purpose at a named point, for recovery drills (CHAINSCOPE_FAULT)."""
    if os.environ.get("CHAINSCOPE_FAULT") == point:
        os._exit(FAULT_EXIT)


def plan_ranges(next_start: int, head: int, size: int) -> list[tuple[int, int]]:
    """Inclusive windows from next_start to head; the last one may be shorter."""
    if size < 1:
        raise ChainScopeError("窗口大小必须为正数")
    return [
        (start, min(start + size - 1, head))
        for start in range(next_start, head + 1, size)
    ]


def split_range(start: int, end: int) -> tuple[tuple[int, int], tuple[int, int]]:
    if end <= start:
        raise ChainScopeError("单个区块的窗口无法再拆分")
    middle = (start + end) // 2
    return (start, middle), (middle + 1, end)


def contiguous_through(first_block: int, rows) -> int | None:
    """Last block of the unbroken run of done windows that begins at first_block.

    rows: (start_block, end_block, status) ordered by start_block.
    """
    through = None
    expected = first_block
    for start, end, status in rows:
        if start != expected or status != "done":
            break
        through = end
        expected = end + 1
    return through


def create_job(
    engine,
    job_id: str,
    token: str,
    *,
    source: str,
    start_block: int,
    window_size: int,
    parser_version: int,
    decimals: int | None,
    deploy_block: int | None = None,
    end_block: int | None = None,
) -> dict:
    if not 1 <= window_size <= 10_000_000:
        raise ChainScopeError("窗口大小需在 1 到 10,000,000 个区块之间")
    if end_block is not None and end_block < start_block:
        raise ChainScopeError("结束区块早于起始区块")
    row = {
        "job_id": job_id,
        "chain_id": 56,
        "token_address": token,
        "token_decimals": decimals,
        "source": source,
        "start_block": start_block,
        "deploy_block": deploy_block,
        "end_block": end_block,
        "window_size": window_size,
        "parser_version": parser_version,
        "verified_through": None,
        "status": "active",
        "created_at_utc": utcnow(),
    }
    with engine.begin() as connection:
        # One job per token: holder balances are kept per token.
        found = (
            connection.execute(
                sa.select(jobs).where(
                    (jobs.c.job_id == job_id)
                    | ((jobs.c.chain_id == 56) & (jobs.c.token_address == token))
                )
            )
            .mappings()
            .first()
        )
        if found is not None and found["job_id"] == job_id:
            raise ChainScopeError(
                f"任务 {job_id} 已存在；继续采集请运行 sync，会从已保存的进度接着做"
            )
        if found is not None:
            raise ChainScopeError(
                f"已有任务 {found['job_id']} 采集同一代币；不会重复创建"
            )
        connection.execute(jobs.insert().values(**row))
    return row


def load_job(connection, job_id: str, *, lock: bool = False) -> dict:
    """The job row; lock=True holds it (FOR UPDATE) until the transaction ends."""
    query = sa.select(jobs).where(jobs.c.job_id == job_id)
    if lock:
        query = query.with_for_update()
    job = connection.execute(query).mappings().one_or_none()
    if job is None:
        raise ChainScopeError(f"没有任务 {job_id}；先执行 job-create")
    return dict(job)


@contextmanager
def job_lock(engine, job_id: str):
    """One runner per job. MySQL frees the lock if this connection dies."""
    name = f"chainscope:job:{job_id}"
    connection = engine.connect()
    try:
        if connection.scalar(sa.text("SELECT GET_LOCK(:n, 0)"), {"n": name}) != 1:
            raise ChainScopeError(f"任务 {job_id} 已有进程在运行；不重复启动")
        try:
            yield
        finally:
            connection.execute(sa.text("SELECT RELEASE_LOCK(:n)"), {"n": name})
    finally:
        connection.close()


def recover_abandoned(engine, job_id: str) -> int:
    """With the job lock held, any 'running' window was left by a dead process."""
    now = utcnow()
    with engine.begin() as connection:
        reset = connection.execute(
            windows.update()
            .where(windows.c.job_id == job_id, windows.c.status == "running")
            .values(status="failed", error_type="abandoned", updated_at_utc=now)
        ).rowcount
        connection.execute(
            batches.update()
            .where(batches.c.job_id == job_id, batches.c.status == "running")
            .values(status="failed", error_type="abandoned", finished_at_utc=now)
        )
    return reset


def plan_windows(engine, job_id: str, head: int) -> int:
    """Append pending windows from the last planned block up to head (or end)."""
    with engine.begin() as connection:
        job = load_job(connection, job_id)
        last = connection.scalar(
            sa.select(sa.func.max(windows.c.end_block)).where(
                windows.c.job_id == job_id
            )
        )
        if job["end_block"] is not None:
            head = min(head, job["end_block"])
        next_start = job["start_block"] if last is None else last + 1
        ranges = plan_ranges(next_start, head, job["window_size"])
        now = utcnow()
        if ranges:
            connection.execute(
                windows.insert(),
                [
                    {
                        "job_id": job_id,
                        "start_block": start,
                        "end_block": end,
                        "status": "pending",
                        "updated_at_utc": now,
                    }
                    for start, end in ranges
                ],
            )
    return len(ranges)


def next_window(engine, job_id: str) -> dict | None:
    """Earliest unfinished window not waiting for a person, holes first."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                sa.select(windows)
                .where(
                    windows.c.job_id == job_id,
                    (windows.c.status == "pending")
                    | (
                        (windows.c.status == "failed")
                        & (
                            windows.c.error_type.is_(None)
                            | windows.c.error_type.not_in(QUARANTINE)
                        )
                    ),
                )
                .order_by(windows.c.start_block)
                .limit(1)
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def begin_window(engine, job_id: str, start: int, attempt_id: str) -> None:
    with engine.begin() as connection:
        changed = connection.execute(
            windows.update()
            .where(
                windows.c.job_id == job_id,
                windows.c.start_block == start,
                windows.c.status.in_(("pending", "failed")),
            )
            .values(
                status="running",
                attempt_id=attempt_id,
                attempts=windows.c.attempts + 1,
                error_type=None,
                updated_at_utc=utcnow(),
            )
        ).rowcount
    if changed != 1:
        raise ChainScopeError("窗口状态已变化，无法开始采集")


def split_window(engine, job_id: str, start: int, end: int) -> tuple[int, int]:
    """Halve a window in place; the second half becomes a new pending window."""
    (_, middle), (second, _) = split_range(start, end)
    now = utcnow()
    with engine.begin() as connection:
        connection.execute(
            windows.update()
            .where(windows.c.job_id == job_id, windows.c.start_block == start)
            .values(
                end_block=middle,
                status="pending",
                error_type=None,
                updated_at_utc=now,
            )
        )
        connection.execute(
            windows.insert().values(
                job_id=job_id,
                start_block=second,
                end_block=end,
                status="pending",
                updated_at_utc=now,
            )
        )
    return middle, second


def fail_window(engine, job_id: str, start: int, error_type: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            windows.update()
            .where(
                windows.c.job_id == job_id,
                windows.c.start_block == start,
                windows.c.status == "running",
            )
            .values(
                status="failed", error_type=error_type[:64], updated_at_utc=utcnow()
            )
        )


def _existing(connection, keys: list[tuple[str, int]]) -> dict:
    found = {}
    for offset in range(0, len(keys), CHUNK):
        chunk = keys[offset : offset + CHUNK]
        rows = connection.execute(
            sa.select(transfers).where(
                transfers.c.chain_id == 56,
                sa.tuple_(transfers.c.tx_hash, transfers.c.log_index).in_(chunk),
            )
        ).mappings()
        for row in rows:
            found[(row["tx_hash"], row["log_index"])] = row
    return found


def _advance(connection, job_id: str) -> int | None:
    job = load_job(connection, job_id, lock=True)
    through = job["verified_through"]
    first = job["start_block"] if through is None else through + 1
    rows = connection.execute(
        sa.select(windows.c.start_block, windows.c.end_block, windows.c.status)
        .where(windows.c.job_id == job["job_id"], windows.c.start_block >= first)
        .order_by(windows.c.start_block)
    ).all()
    newer = contiguous_through(first, rows)
    if newer is not None:
        connection.execute(
            jobs.update()
            .where(jobs.c.job_id == job["job_id"])
            .values(verified_through=newer)
        )
        return newer
    return through


def _conflicts(existing: dict, events: list[dict]) -> list[dict]:
    diffs = []
    for event in events:
        row = existing.get((event["tx_hash"], event["log_index"]))
        if row is None:
            continue
        changed = {
            field: {"stored": str(row[field]), "fetched": str(value)}
            for field, value in event.items()
            if field in row and row[field] != value
        }
        if changed:
            diffs.append(
                {
                    "kind": "field_conflict",
                    "tx_hash": event["tx_hash"],
                    "log_index": event["log_index"],
                    "detail": {"fields": changed},
                }
            )
    return diffs


def _relink(connection, job_id: str, start: int, end: int, event_ids: set) -> list:
    """Make the window's links exactly event_ids; return the ids dropped."""
    linked = set(
        connection.scalars(
            sa.select(window_events.c.event_id).where(
                window_events.c.job_id == job_id,
                window_events.c.start_block == start,
            )
        )
    )
    dropped = sorted(linked - event_ids)
    for offset in range(0, len(dropped), CHUNK):
        connection.execute(
            window_events.delete().where(
                window_events.c.job_id == job_id,
                window_events.c.start_block == start,
                window_events.c.event_id.in_(dropped[offset : offset + CHUNK]),
            )
        )
    added = [
        {"job_id": job_id, "start_block": start, "event_id": event_id}
        for event_id in sorted(event_ids - linked)
    ]
    for offset in range(0, len(added), CHUNK):
        connection.execute(window_events.insert(), added[offset : offset + CHUNK])
    return dropped


def _dropped_evidence(connection, job_id, start, end, dropped: list) -> None:
    """Record links removed by a redo; the transfer rows themselves are kept."""
    rows = []
    for offset in range(0, len(dropped), CHUNK):
        rows += connection.execute(
            sa.select(
                transfers.c.tx_hash, transfers.c.log_index, transfers.c.block_number
            ).where(
                transfers.c.event_id.in_(dropped[offset : offset + CHUNK]),
                # Events outside the range moved to the other half of a split.
                transfers.c.block_number.between(start, end),
            )
        ).all()
    if rows:
        connection.execute(
            window_diffs.insert(),
            [
                {
                    "job_id": job_id,
                    "start_block": start,
                    "end_block": end,
                    "found_at_utc": utcnow(),
                    "kind": "dropped_on_redo",
                    "tx_hash": tx_hash,
                    "log_index": log_index,
                    "detail": {"block_number": block},
                }
                for tx_hash, log_index, block in rows
            ],
        )


def _store(
    engine,
    job,
    window,
    attempt_id,
    events,
    tally,
    source_head,
    extra=None,
    swap_rows=None,
) -> dict:
    job_id, start, end = job["job_id"], window["start_block"], window["end_block"]
    extra = dict(extra or {})
    with engine.begin() as connection:
        locked = (
            connection.execute(
                sa.select(windows.c.status, windows.c.attempt_id)
                .where(windows.c.job_id == job_id, windows.c.start_block == start)
                .with_for_update()
            )
            .mappings()
            .one()
        )
        if locked["status"] != "running" or locked["attempt_id"] != attempt_id:
            raise ChainScopeError("窗口已被其他尝试接管，本次结果不写入")
        keys = [(e["tx_hash"], e["log_index"]) for e in events]
        if len(set(keys)) != len(keys):
            raise ChainScopeError("同一窗口返回了重复事件")
        existing = _existing(connection, keys)
        conflicts = _conflicts(existing, events)
        if conflicts:
            raise EventConflict(
                f"窗口 {start}-{end} 有 {len(conflicts)} 条事件与数据库已存内容不同"
                "（区块、时间、地址或金额）；不会覆盖，已隔离该窗口",
                conflicts,
            )
        fresh = [e for e in events if (e["tx_hash"], e["log_index"]) not in existing]
        for offset in range(0, len(fresh), CHUNK):
            connection.execute(transfers.insert(), fresh[offset : offset + CHUNK])
        if fresh:
            existing.update(
                _existing(connection, [(e["tx_hash"], e["log_index"]) for e in fresh])
            )
        ids = {existing[key]["event_id"] for key in keys}
        dropped = _relink(connection, job_id, start, end, ids)
        _dropped_evidence(connection, job_id, start, end, dropped)
        if swap_rows is not None:
            # Swaps are derived data: replace the window's set as a whole.
            connection.execute(
                swaps.delete().where(
                    swaps.c.job_id == job_id,
                    swaps.c.block_number.between(start, end),
                )
            )
            rows = [{**s, "job_id": job_id} for s in swap_rows]
            for offset in range(0, len(rows), CHUNK):
                connection.execute(swaps.insert(), rows[offset : offset + CHUNK])
        # Read back inside the transaction: a mismatch rolls everything back.
        stored = _window_tally(connection, job_id, start, tally.version)
        stored_swaps = connection.scalar(
            sa.select(sa.func.count())
            .select_from(swaps)
            .where(swaps.c.job_id == job_id, swaps.c.block_number.between(start, end))
        )
        if not stored.matches(
            tally.events, tally.amount, tally.fingerprint_bytes()
        ) or (swap_rows is not None and stored_swaps != len(swap_rows)):
            raise ReadbackMismatch(
                f"窗口 {start}-{end} 写入后读回与采集结果不一致；已回滚，未标记完成",
                [
                    {
                        "kind": "readback_mismatch",
                        "detail": {
                            "fetched_events": tally.events,
                            "stored_events": stored.events,
                            "fetched_swaps": None
                            if swap_rows is None
                            else len(swap_rows),
                            "stored_swaps": stored_swaps,
                        },
                    }
                ],
            )
        fault("before_done")
        now = utcnow()
        connection.execute(
            windows.update()
            .where(windows.c.job_id == job_id, windows.c.start_block == start)
            .values(
                status="done",
                swap_count=None if swap_rows is None else len(swap_rows),
                event_count=len(events),
                inserted_count=len(fresh),
                amount_sum=str(tally.amount),
                fingerprint=tally.fingerprint_bytes(),
                fingerprint_version=tally.version,
                source_head=source_head,
                parser_version=extra.pop("parser_version", job["parser_version"]),
                error_type=None,
                updated_at_utc=now,
                **extra,
            )
        )
        connection.execute(
            batches.update()
            .where(batches.c.batch_id == attempt_id)
            .values(
                status="succeeded",
                finished_at_utc=now,
                event_count=len(events),
                inserted_count=len(fresh),
                existing_count=len(events) - len(fresh),
            )
        )
        through = _advance(connection, job_id)
        fault("before_commit")
    fault("after_commit")
    return {
        "inserted": len(fresh),
        "existing": len(events) - len(fresh),
        "dropped_links": len(dropped),
        "through": through,
    }


def _committed(engine, job_id: str, start: int, attempt_id: str) -> bool:
    with engine.connect() as connection:
        row = connection.execute(
            sa.select(windows.c.status, windows.c.attempt_id).where(
                windows.c.job_id == job_id, windows.c.start_block == start
            )
        ).first()
    return row is not None and row.status == "done" and row.attempt_id == attempt_id


def store_window(
    engine,
    job,
    window,
    attempt_id,
    events,
    tally,
    source_head,
    *,
    sleep,
    extra=None,
    swap_rows=None,
) -> dict:
    """Commit one window atomically; retry deadlocks; resolve unknown commits."""
    for attempt in range(3):
        try:
            return _store(
                engine,
                job,
                window,
                attempt_id,
                events,
                tally,
                source_head,
                extra,
                swap_rows,
            )
        except OperationalError as exc:
            code = exc.orig.args[0] if exc.orig is not None and exc.orig.args else None
            if code in LOST and _committed(
                engine, job["job_id"], window["start_block"], attempt_id
            ):
                # The commit reached MySQL even though the reply was lost; the
                # readback ran inside that transaction, so the window is verified.
                with engine.connect() as connection:
                    current = load_job(connection, job["job_id"])
                return {
                    "committed_unacknowledged": True,
                    "through": current["verified_through"],
                }
            if code not in RETRYABLE | LOST or attempt == 2:
                raise
            sleep(2**attempt)
    raise AssertionError("unreachable retry state")


def _window_tally(connection, job_id: str, start: int, version: int) -> Tally:
    """Recompute a window's totals from its linked events, streaming rows."""
    tally = Tally(version)
    query = (
        sa.select(
            transfers.c.tx_hash,
            transfers.c.log_index,
            transfers.c.from_address,
            transfers.c.to_address,
            transfers.c.amount_raw,
            transfers.c.block_number,
        )
        .join(window_events, window_events.c.event_id == transfers.c.event_id)
        .where(window_events.c.job_id == job_id, window_events.c.start_block == start)
    )
    rows = connection.execution_options(yield_per=5000).execute(query)
    for tx_hash, index, sender, receiver, amount, block in rows:
        tally.add(tx_hash, index, sender, receiver, int(amount), block)
    return tally


def window_tally(
    engine, job_id: str, start: int, version: int = FINGERPRINT_VERSION
) -> Tally:
    with engine.connect() as connection:
        return _window_tally(connection, job_id, start, version)


def window_rows(engine, job_id: str, start: int, end: int) -> tuple[dict, dict]:
    """Stored Transfers (linked to the window) and swaps of one window, by key."""
    with engine.connect() as connection:
        events = {
            (r["tx_hash"], r["log_index"]): dict(r)
            for r in connection.execute(
                sa.select(transfers)
                .join(window_events, window_events.c.event_id == transfers.c.event_id)
                .where(
                    window_events.c.job_id == job_id,
                    window_events.c.start_block == start,
                )
            ).mappings()
        }
        stored_swaps = {
            (r["tx_hash"], r["log_index"]): dict(r)
            for r in connection.execute(
                sa.select(swaps).where(
                    swaps.c.job_id == job_id, swaps.c.block_number.between(start, end)
                )
            ).mappings()
        }
    return events, stored_swaps


def job_status(
    engine,
    job_id: str,
    *,
    max_lag_blocks: int | None = None,
    head: int | None = None,
    head_observed_at=None,
    head_error: str | None = None,
) -> dict:
    """Progress of a job. `head` is the live finalized height, if it could be read."""
    with engine.connect() as connection:
        job = load_job(connection, job_id)
        counts = dict(
            connection.execute(
                sa.select(windows.c.status, sa.func.count())
                .where(windows.c.job_id == job_id)
                .group_by(windows.c.status)
            ).all()
        )
        span = connection.execute(
            sa.select(
                sa.func.min(windows.c.start_block),
                sa.func.max(windows.c.end_block),
                sa.func.max(windows.c.source_head),
                sa.func.coalesce(sa.func.sum(windows.c.event_count), 0),
            ).where(windows.c.job_id == job_id)
        ).one()
        last_done = connection.scalar(
            sa.select(sa.func.max(windows.c.updated_at_utc)).where(
                windows.c.job_id == job_id, windows.c.status == "done"
            )
        )
        remaining = connection.scalar(
            sa.select(
                sa.func.coalesce(
                    sa.func.sum(windows.c.end_block - windows.c.start_block + 1), 0
                )
            ).where(windows.c.job_id == job_id, windows.c.status != "done")
        )
        failed = connection.execute(
            sa.select(
                windows.c.start_block,
                windows.c.end_block,
                windows.c.attempts,
                windows.c.error_type,
            )
            .where(windows.c.job_id == job_id, windows.c.status == "failed")
            .order_by(windows.c.start_block)
            .limit(10)
        ).mappings()
        failed = [
            {**row, "quarantined": row["error_type"] in QUARANTINE} for row in failed
        ]
        reconcile = dict(
            connection.execute(
                sa.select(windows.c.reconcile, sa.func.count())
                .where(windows.c.job_id == job_id, windows.c.status == "done")
                .group_by(windows.c.reconcile)
            ).all()
        )
        diffs = connection.scalar(
            sa.select(sa.func.count())
            .select_from(window_diffs)
            .where(window_diffs.c.job_id == job_id)
        )
    through = job["verified_through"]
    target = head
    if head is not None and job["end_block"] is not None:
        target = min(head, job["end_block"])
    if target is None:
        lag = None
    else:
        lag = target - (job["start_block"] - 1 if through is None else through)
        lag = max(lag, 0)
    if max_lag_blocks is None:
        behind = None
    elif lag is None:
        behind = "unknown"
    else:
        behind = lag > max_lag_blocks
    return {
        "job_id": job_id,
        "token_address": job["token_address"],
        "source": job["source"],
        "status": job["status"],
        "deploy_block": job["deploy_block"],
        "start_block": job["start_block"],
        "end_block": job["end_block"],
        "verified_through": through,
        "planned_through": span[1],
        "current_head": head,
        "head_observed_at_utc": head_observed_at,
        "head_error": head_error,
        "head_at_last_run": span[2],
        "last_window_done_at_utc": last_done,
        "events_in_done_windows": int(span[3]),
        "windows": {
            s: counts.get(s, 0) for s in ("done", "pending", "running", "failed")
        },
        "blocks_remaining_in_planned_windows": int(remaining),
        # Measured against the live head; None when it could not be read.
        "lag_blocks": lag,
        "behind": behind,
        "reconcile": {str(k): v for k, v in reconcile.items()},
        "recorded_diffs": diffs,
        "failed_windows": failed,
    }


def record_diffs(engine, job_id: str, start: int, end: int, diffs: list[dict]) -> int:
    now = utcnow()
    rows = [
        {
            "job_id": job_id,
            "start_block": start,
            "end_block": end,
            "found_at_utc": now,
            "kind": d["kind"],
            "tx_hash": d.get("tx_hash"),
            "log_index": d.get("log_index"),
            "detail": d.get("detail", {}),
        }
        for d in diffs
    ]
    with engine.begin() as connection:
        for offset in range(0, len(rows), CHUNK):
            connection.execute(window_diffs.insert(), rows[offset : offset + CHUNK])
    return len(rows)


def _rewind(connection, job_id: str, first_block: int) -> None:
    """Pull the watermark back so it no longer covers first_block.

    Balances that covered the rewound blocks are corrected in the same
    transaction: the rewound range is subtracted using the links that were
    added (they cannot have changed while their windows were done), or, for a
    long range, the balances are cleared for a chunked rebuild.
    """
    job = load_job(connection, job_id, lock=True)
    through = job["verified_through"]
    if through is None or through < first_block:
        return
    value = None if first_block <= job["start_block"] else first_block - 1
    changes = {"verified_through": value}
    mark = job["balances_through"]
    if mark is not None and (value is None or value < mark):
        if value is not None and mark - value <= BALANCE_REWIND_LIMIT:
            balances.apply_range(connection, job, value + 1, mark, -1)
            changes["balances_through"] = value
        else:
            balances.clear(connection, job["token_address"])
            changes["balances_through"] = None
    connection.execute(jobs.update().where(jobs.c.job_id == job_id).values(**changes))


def invalidate_window(engine, job_id: str, start: int, error_type: str) -> None:
    """A done window turned out wrong: fail it and rewind the watermark."""
    with engine.begin() as connection:
        connection.execute(
            windows.update()
            .where(windows.c.job_id == job_id, windows.c.start_block == start)
            .values(
                status="failed", error_type=error_type[:64], updated_at_utc=utcnow()
            )
        )
        _rewind(connection, job_id, start)


def reset_windows(
    engine,
    job_id: str,
    *,
    below_version: int | None = None,
    first: int | None = None,
    last: int | None = None,
    connection=None,
) -> int:
    """Send windows back to pending: an old parser version, or a block range.

    A block range also releases quarantined windows in it, after investigation.
    Pass `connection` to make the reset part of a larger transaction.
    """
    if connection is None:
        with engine.begin() as connection:
            return reset_windows(
                engine,
                job_id,
                below_version=below_version,
                first=first,
                last=last,
                connection=connection,
            )
    condition = [windows.c.job_id == job_id]
    if below_version is not None:
        condition += [
            windows.c.status == "done",
            sa.func.coalesce(windows.c.parser_version, 0) < below_version,
        ]
    else:
        condition.append(windows.c.status.in_(("done", "failed")))
    if first is not None:
        condition.append(windows.c.end_block >= first)
    if last is not None:
        condition.append(windows.c.start_block <= last)
    earliest = connection.scalar(
        sa.select(sa.func.min(windows.c.start_block)).where(*condition)
    )
    if earliest is None:
        return 0
    count = connection.execute(
        windows.update()
        .where(*condition)
        .values(
            status="pending", reconcile=None, error_type=None, updated_at_utc=utcnow()
        )
    ).rowcount
    _rewind(connection, job_id, earliest)
    return count


def pool_set_hash(addresses) -> str:
    """Short, order-independent name of a set of pool addresses."""
    joined = ",".join(sorted(addresses))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def expected_pool_set(pools: dict, end_block: int) -> str:
    """Pools a window ending at end_block must have been collected with."""
    return pool_set_hash(
        address for address, pool in pools.items() if pool["created_block"] <= end_block
    )


def reset_stale_pool_windows(engine, job_id: str, pools: dict, connection=None) -> int:
    """Done windows collected with another pool set go back to pending.

    This is the recovery net for pool changes: whatever step a discover, add,
    enable or disable stopped at, the next sync redoes the windows whose swaps
    do not match the pools enabled now. Pass `connection` to join a transaction.
    """
    if connection is None:
        with engine.begin() as connection:
            return reset_stale_pool_windows(engine, job_id, pools, connection)
    rows = connection.execute(
        sa.select(windows.c.start_block, windows.c.end_block, windows.c.pool_set)
        .where(windows.c.job_id == job_id, windows.c.status == "done")
        .with_for_update()
    ).all()
    stale = [
        start
        for start, end, stored in rows
        if (stored or pool_set_hash(())) != expected_pool_set(pools, end)
    ]
    for offset in range(0, len(stale), CHUNK):
        connection.execute(
            windows.update()
            .where(
                windows.c.job_id == job_id,
                windows.c.start_block.in_(stale[offset : offset + CHUNK]),
            )
            .values(status="pending", reconcile=None, updated_at_utc=utcnow())
        )
    if stale:
        _rewind(connection, job_id, min(stale))
    return len(stale)


def sample_done_windows(engine, job_id: str, count: int, seed=None) -> list[dict]:
    with engine.connect() as connection:
        rows = (
            connection.execute(
                sa.select(windows)
                .where(windows.c.job_id == job_id, windows.c.status == "done")
                .order_by(windows.c.start_block)
            )
            .mappings()
            .all()
        )
    rows = [dict(r) for r in rows]
    if count >= len(rows):
        return rows
    return sorted(
        random.Random(seed).sample(rows, count), key=lambda r: r["start_block"]
    )


def verify_job(engine, job_id: str) -> dict:
    """Internal consistency of MySQL: no second source is consulted.

    Per done window: linked Transfers against the stored count, amount sum and
    fingerprint (with the window's own fingerprint version), and the stored
    swap count. Per block: one block hash and one time. External changes are
    found by `recheck`, which fetches the windows again.
    """
    with engine.connect() as connection:
        done = connection.execute(
            sa.select(
                windows.c.start_block,
                windows.c.end_block,
                windows.c.event_count,
                windows.c.amount_sum,
                windows.c.fingerprint,
                windows.c.fingerprint_version,
                windows.c.swap_count,
            )
            .where(windows.c.job_id == job_id, windows.c.status == "done")
            .order_by(windows.c.start_block)
        ).all()
        swap_counts = dict(
            connection.execute(
                sa.select(windows.c.start_block, sa.func.count(swaps.c.swap_id))
                .select_from(
                    windows.outerjoin(
                        swaps,
                        (swaps.c.job_id == windows.c.job_id)
                        & swaps.c.block_number.between(
                            windows.c.start_block, windows.c.end_block
                        ),
                    )
                )
                .where(windows.c.job_id == job_id, windows.c.status == "done")
                .group_by(windows.c.start_block)
            ).all()
        )
        split_blocks = (
            connection.execute(
                sa.select(transfers.c.block_number)
                .join(window_events, window_events.c.event_id == transfers.c.event_id)
                .where(window_events.c.job_id == job_id)
                .group_by(transfers.c.block_number)
                .having(
                    (sa.func.count(sa.distinct(transfers.c.block_hash)) > 1)
                    | (sa.func.count(sa.distinct(transfers.c.block_time_utc)) > 1)
                )
                .limit(20)
            )
            .scalars()
            .all()
        )
    bad = []
    events = 0
    versions = set()
    for start, end, count, amount, fingerprint, version, swap_count in done:
        version = version or 1
        versions.add(version)
        tally = window_tally(engine, job_id, start, version)
        events += tally.events
        problems = []
        if not tally.matches(count, int(amount), fingerprint):
            problems.append("transfers")
        if swap_count is not None and swap_counts.get(start, 0) != swap_count:
            problems.append("swap_count")
        if problems:
            bad.append(
                {
                    "window": [start, end],
                    "problems": problems,
                    "stored_events": count,
                    "database_events": tally.events,
                    "stored_swaps": swap_count,
                    "database_swaps": swap_counts.get(start, 0),
                }
            )
    return {
        "job_id": job_id,
        "scope": "数据库内部一致性：Transfer 指纹、Swap 条数、同一区块的哈希和时间；"
        "不访问外部数据源（外部复查用 recheck）",
        "windows_checked": len(done),
        "fingerprint_versions": sorted(versions),
        "events": events,
        "mismatched_windows": bad,
        "blocks_with_conflicting_header": split_blocks,
        "ok": not bad and not split_blocks,
    }
