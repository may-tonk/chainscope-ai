"""Scan jobs and coverage windows: durable progress for resumable collection.

A job is one token on one source. Its block range is cut into windows; a window
is collected and committed as a unit, so it is also the resume point. The job's
`verified_through` only advances over an unbroken run of done windows.
"""

from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from .database import utcnow
from .errors import ChainScopeError
from .models import batches, jobs, transfers, window_events, windows
from .validator import Tally

CHUNK = 1000
# MySQL errors after which the whole window transaction can safely run again.
RETRYABLE = {1205, 1213}  # lock wait timeout, deadlock
LOST = {2006, 2013}  # server gone away, lost connection


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
) -> dict:
    if not 1 <= window_size <= 10_000_000:
        raise ChainScopeError("窗口大小需在 1 到 10,000,000 个区块之间")
    row = {
        "job_id": job_id,
        "chain_id": 56,
        "token_address": token,
        "token_decimals": decimals,
        "source": source,
        "start_block": start_block,
        "window_size": window_size,
        "parser_version": parser_version,
        "verified_through": None,
        "status": "active",
        "created_at_utc": utcnow(),
    }
    with engine.begin() as connection:
        found = (
            connection.execute(
                sa.select(jobs).where(
                    (jobs.c.job_id == job_id)
                    | (
                        (jobs.c.chain_id == 56)
                        & (jobs.c.token_address == token)
                        & (jobs.c.source == source)
                    )
                )
            )
            .mappings()
            .first()
        )
        if found is not None:
            raise ChainScopeError(
                f"已有任务 {found['job_id']} 采集同一代币和数据源；不会重复创建"
            )
        connection.execute(jobs.insert().values(**row))
    return row


def load_job(connection, job_id: str) -> dict:
    job = (
        connection.execute(sa.select(jobs).where(jobs.c.job_id == job_id))
        .mappings()
        .one_or_none()
    )
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
    """Append pending windows from the last planned block up to head."""
    with engine.begin() as connection:
        job = load_job(connection, job_id)
        last = connection.scalar(
            sa.select(sa.func.max(windows.c.end_block)).where(
                windows.c.job_id == job_id
            )
        )
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
    """Earliest unfinished window, so holes are filled before newer data."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                sa.select(windows)
                .where(
                    windows.c.job_id == job_id,
                    windows.c.status.in_(("pending", "failed")),
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
    job = load_job(connection, job_id)
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


def _store(engine, job, window, attempt_id, events, tally, source_head) -> dict:
    with engine.begin() as connection:
        locked = (
            connection.execute(
                sa.select(windows.c.status, windows.c.attempt_id)
                .where(
                    windows.c.job_id == job["job_id"],
                    windows.c.start_block == window["start_block"],
                )
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
        fresh = []
        for event in events:
            row = existing.get((event["tx_hash"], event["log_index"]))
            if row is None:
                fresh.append(event)
            elif any(row[field] != value for field, value in event.items()):
                raise ChainScopeError(
                    "数据库同一事件内容冲突；本窗口已回滚，需调查链重组或数据来源"
                )
        for offset in range(0, len(fresh), CHUNK):
            connection.execute(transfers.insert(), fresh[offset : offset + CHUNK])
        if fresh:
            existing.update(
                _existing(connection, [(e["tx_hash"], e["log_index"]) for e in fresh])
            )
        links = [
            {
                "job_id": job["job_id"],
                "start_block": window["start_block"],
                "event_id": existing[key]["event_id"],
            }
            for key in keys
        ]
        for offset in range(0, len(links), CHUNK):
            connection.execute(
                window_events.insert().prefix_with("IGNORE"),
                links[offset : offset + CHUNK],
            )
        now = utcnow()
        connection.execute(
            windows.update()
            .where(
                windows.c.job_id == job["job_id"],
                windows.c.start_block == window["start_block"],
            )
            .values(
                status="done",
                event_count=len(events),
                inserted_count=len(fresh),
                amount_sum=str(tally.amount),
                fingerprint=tally.fingerprint_bytes(),
                source_head=source_head,
                parser_version=job["parser_version"],
                error_type=None,
                updated_at_utc=now,
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
        through = _advance(connection, job["job_id"])
    return {
        "inserted": len(fresh),
        "existing": len(events) - len(fresh),
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
    engine, job, window, attempt_id, events, tally, source_head, *, sleep
) -> dict:
    """Commit one window atomically; retry deadlocks; resolve unknown commits."""
    for attempt in range(3):
        try:
            return _store(engine, job, window, attempt_id, events, tally, source_head)
        except OperationalError as exc:
            code = exc.orig.args[0] if exc.orig is not None and exc.orig.args else None
            if code in LOST and _committed(
                engine, job["job_id"], window["start_block"], attempt_id
            ):
                # The commit reached MySQL even though the reply was lost.
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


def window_tally(engine, job_id: str, start: int) -> Tally:
    """Recompute a window's totals from MySQL, streaming rows."""
    tally = Tally()
    query = (
        sa.select(
            transfers.c.tx_hash,
            transfers.c.log_index,
            transfers.c.from_address,
            transfers.c.to_address,
            transfers.c.amount_raw,
        )
        .join(window_events, window_events.c.event_id == transfers.c.event_id)
        .where(window_events.c.job_id == job_id, window_events.c.start_block == start)
    )
    with engine.connect() as connection:
        rows = connection.execution_options(yield_per=5000).execute(query)
        for tx_hash, index, sender, receiver, amount in rows:
            tally.add(tx_hash, index, sender, receiver, int(amount))
    return tally


def job_status(engine, job_id: str) -> dict:
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
        failed = [dict(row) for row in failed]
    through = job["verified_through"]
    return {
        "job_id": job_id,
        "token_address": job["token_address"],
        "source": job["source"],
        "status": job["status"],
        "start_block": job["start_block"],
        "verified_through": through,
        "planned_through": span[1],
        "last_source_head": span[2],
        "events_in_done_windows": int(span[3]),
        "windows": {
            s: counts.get(s, 0) for s in ("done", "pending", "running", "failed")
        },
        "blocks_remaining": int(remaining),
        "lag_blocks": None if through is None or span[2] is None else span[2] - through,
        "failed_windows": failed,
    }
