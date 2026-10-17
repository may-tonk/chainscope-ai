"""Independent, atomic Swap coverage; never rewind or re-fetch Transfers."""

import hashlib
import json

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import insert as upsert

from .database import utcnow
from .dex import attribute_all, block_swaps, load_pools, swap_log_filter
from .errors import BlockTooLarge, ChainScopeError, ReadbackMismatch
from .models import jobs, supplement_jobs, swap_windows, swaps, transfers, window_events
from .sqd import BLOCK_FIELDS, LOG_FIELDS, TooManyEvents, _blocks_or_split


def enable(connection, job_id):
    connection.execute(
        upsert(supplement_jobs)
        .values(job_id=job_id, updated_at_utc=utcnow())
        .on_duplicate_key_update(job_id=job_id)
    )


def independent(connection, job_id):
    return (
        connection.scalar(
            sa.select(supplement_jobs.c.job_id).where(
                supplement_jobs.c.job_id == job_id
            )
        )
        is not None
    )


def advance(connection, job_id):
    from .jobs import contiguous_through, load_job

    job = load_job(connection, job_id)
    rows = connection.execute(
        sa.select(
            swap_windows.c.start_block, swap_windows.c.end_block, swap_windows.c.status
        )
        .where(swap_windows.c.job_id == job_id)
        .order_by(swap_windows.c.start_block)
    ).all()
    mark = contiguous_through(job["start_block"], rows)
    if mark is not None:
        mark = (
            min(mark, job["verified_through"])
            if job["verified_through"] is not None
            else None
        )
    connection.execute(
        supplement_jobs.update()
        .where(supplement_jobs.c.job_id == job_id)
        .values(verified_through=mark, updated_at_utc=utcnow())
    )
    return mark


def rewind(connection, job_id, start):
    if not independent(connection, job_id):
        return
    connection.execute(
        swap_windows.update()
        .where(swap_windows.c.job_id == job_id, swap_windows.c.end_block >= start)
        .values(status="pending", error_type=None)
    )
    advance(connection, job_id)


def reset_pools(connection, job_id, pools):
    from .jobs import expected_pool_set

    stale = []
    for row in connection.execute(
        sa.select(swap_windows).where(
            swap_windows.c.job_id == job_id, swap_windows.c.status == "done"
        )
    ).mappings():
        if row["pool_set"] != expected_pool_set(pools, row["end_block"]):
            stale.append(row["start_block"])
    if stale:
        connection.execute(
            swap_windows.update()
            .where(
                swap_windows.c.job_id == job_id, swap_windows.c.start_block.in_(stale)
            )
            .values(status="pending", error_type=None)
        )
        advance(connection, job_id)
    return len(stale)


def progress(connection, job_id, head=None):
    row = (
        connection.execute(
            sa.select(supplement_jobs).where(supplement_jobs.c.job_id == job_id)
        )
        .mappings()
        .first()
    )
    if row is None:
        return {}
    job = (
        connection.execute(sa.select(jobs).where(jobs.c.job_id == job_id))
        .mappings()
        .one()
    )
    mark = row["verified_through"]
    if job["verified_through"] is None:
        mark = None
    elif mark is not None:
        mark = min(mark, job["verified_through"])
    counts = dict(
        connection.execute(
            sa.select(swap_windows.c.status, sa.func.count())
            .where(swap_windows.c.job_id == job_id)
            .group_by(swap_windows.c.status)
        ).all()
    )
    return {
        "transfer_validation_policy": "alchemy_local",
        "transfers_committed_through": job["verified_through"],
        "swaps_verified_through": mark,
        "swap_windows": counts,
        "swaps_complete": mark is not None and head is not None and mark >= head,
    }


def next_window(engine, job, head):
    from .jobs import plan_ranges

    with engine.begin() as c:
        last = c.scalar(
            sa.select(sa.func.max(swap_windows.c.end_block)).where(
                swap_windows.c.job_id == job["job_id"]
            )
        )
        first = job["start_block"] if last is None else last + 1
        rows = [
            dict(
                job_id=job["job_id"],
                start_block=a,
                end_block=b,
                status="pending",
                updated_at_utc=utcnow(),
            )
            for a, b in plan_ranges(first, head, job["window_size"])
        ]
        if rows:
            c.execute(swap_windows.insert(), rows)
        row = (
            c.execute(
                sa.select(swap_windows)
                .where(
                    swap_windows.c.job_id == job["job_id"],
                    swap_windows.c.end_block <= head,
                    swap_windows.c.status == "pending",
                )
                .order_by(swap_windows.c.start_block)
                .limit(1)
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def split(engine, job_id, window):
    from .jobs import split_range

    (first, middle), (second, last) = split_range(
        window["start_block"], window["end_block"]
    )
    with engine.begin() as c:
        c.execute(
            swap_windows.update()
            .where(swap_windows.c.job_id == job_id, swap_windows.c.start_block == first)
            .values(end_block=middle)
        )
        c.execute(
            swap_windows.insert().values(
                job_id=job_id,
                start_block=second,
                end_block=last,
                status="pending",
                updated_at_utc=utcnow(),
            )
        )


def fingerprint(rows):
    digest = hashlib.sha256()
    fields = tuple(k for k in swaps.c.keys() if k not in ("swap_id", "job_id"))
    for row in sorted(rows, key=lambda r: (r["tx_hash"], r["log_index"])):
        digest.update(
            json.dumps(
                [row[k] for k in fields], default=str, separators=(",", ":")
            ).encode()
        )
        digest.update(b"\n")
    return digest.digest()


def collect(engine, job, window, portal, rpc, pools, limit, hard_limit):
    from .jobs import expected_pool_set
    from .reconcile import sample_swap_receipts

    first, last = window["start_block"], window["end_block"]
    active = {a: p for a, p in pools.items() if p["created_block"] <= last}
    legs = []
    if active:
        query = {
            "type": "evm",
            "fields": {
                "block": BLOCK_FIELDS,
                "log": LOG_FIELDS,
                "transaction": {"hash": True, "from": True},
            },
            "logs": swap_log_filter(active),
        }
        blocks = portal.stream(query, first, last)
        try:
            for block in _blocks_or_split(blocks, first, last):
                legs.extend(block_swaps(block, active))
                if len(legs) > limit and last > first:
                    raise TooManyEvents(len(legs))
                if len(legs) > hard_limit:
                    raise BlockTooLarge("单区块 Swap 超过硬上限")
        finally:
            blocks.close()
    # Read only the already formal Transfers; no external Transfer query.
    events = []
    if legs:
        with engine.connect() as c:
            events = [
                dict(r, amount=int(r["amount_raw"]))
                for r in c.execute(
                    sa.select(transfers)
                    .join(
                        window_events, window_events.c.event_id == transfers.c.event_id
                    )
                    .where(
                        window_events.c.job_id == job["job_id"],
                        transfers.c.block_number.between(first, last),
                    )
                    .limit(hard_limit + 1)
                ).mappings()
            ]
        if len(events) + len(legs) > limit and last > first:
            raise TooManyEvents(len(events) + len(legs))
        if len(events) + len(legs) > hard_limit:
            raise BlockTooLarge("单区块 Swap 与关联转账超过硬上限")
    rows = attribute_all(legs, events)
    sample_swap_receipts(rpc, rows, active, first)
    expected = fingerprint(rows)
    with engine.begin() as c:
        # Lock the source watermark so a concurrent reset cannot admit stale derived data.
        current = (
            c.execute(
                sa.select(jobs).where(jobs.c.job_id == job["job_id"]).with_for_update()
            )
            .mappings()
            .one()
        )
        if current["verified_through"] is None or current["verified_through"] < last:
            raise ChainScopeError("转账连续范围发生变化，暂不提交 Swap")
        actual = load_pools(c, job["job_id"])
        if expected_pool_set(actual, last) != expected_pool_set(pools, last):
            raise ChainScopeError("交易池配置发生变化，稍后重试 Swap")
        c.execute(
            swaps.delete().where(
                swaps.c.job_id == job["job_id"],
                swaps.c.block_number.between(first, last),
            )
        )
        for offset in range(0, len(rows), 1000):
            c.execute(
                swaps.insert(),
                [dict(r, job_id=job["job_id"]) for r in rows[offset : offset + 1000]],
            )
        stored = list(
            c.execute(
                sa.select(swaps).where(
                    swaps.c.job_id == job["job_id"],
                    swaps.c.block_number.between(first, last),
                )
            ).mappings()
        )
        if len(stored) != len(rows) or fingerprint(stored) != expected:
            raise ReadbackMismatch("Swap 事务读回不一致")
        from .jobs import fault

        fault("before_swap_commit")
        c.execute(
            swap_windows.update()
            .where(
                swap_windows.c.job_id == job["job_id"],
                swap_windows.c.start_block == first,
            )
            .values(
                status="done",
                event_count=len(rows),
                fingerprint=expected,
                pool_set=expected_pool_set(pools, last),
                updated_at_utc=utcnow(),
                error_type=None,
            )
        )
        mark = advance(c, job["job_id"])
    return {"swaps": len(rows), "through": mark}


def verify(engine, job_id):
    bad = []
    with engine.connect() as c:
        rows = (
            c.execute(
                sa.select(swap_windows).where(
                    swap_windows.c.job_id == job_id, swap_windows.c.status == "done"
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            stored = list(
                c.execute(
                    sa.select(swaps).where(
                        swaps.c.job_id == job_id,
                        swaps.c.block_number.between(
                            row["start_block"], row["end_block"]
                        ),
                    )
                ).mappings()
            )
            if (
                len(stored) != row["event_count"]
                or fingerprint(stored) != row["fingerprint"]
            ):
                bad.append(row["start_block"])
    return {"ok": not bad, "windows_checked": len(rows), "mismatched_windows": bad}
