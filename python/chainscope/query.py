"""Date-range queries over collected events, honest about partial coverage.

Times are read as Beijing time (UTC+8) unless they carry an offset. A range is
[start, end): the start instant is included and the end instant is not.
"""

import re
import time
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from .errors import ChainScopeError
from .jobs import load_job
from .models import transfers, window_events, windows

BEIJING = timezone(timedelta(hours=8))
ZERO = "0x" + "0" * 40
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_time(text: str) -> datetime:
    """'2026-10-01' or '2026-10-01T08:30[:00][+08:00]'; no offset means Beijing."""
    text = text.strip()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ChainScopeError(
            f"时间格式不正确：{text}（例如 2026-10-01 或 2026-10-01T08:30）"
        ) from None
    if DATE.fullmatch(text) or moment.tzinfo is None:
        moment = moment.replace(tzinfo=BEIJING)
    return moment


def block_range(portal, start: datetime, end: datetime) -> tuple[int, int]:
    """Blocks with timestamps in [start, end)."""
    if end <= start:
        raise ChainScopeError("结束时间必须晚于开始时间")
    first = portal.block_at(int(start.timestamp()))
    after = portal.block_at(int(end.timestamp()))
    return first, after - 1


def coverage(job: dict, first: int, last: int) -> dict:
    through = job["verified_through"]
    if last < job["start_block"]:
        return {"state": "complete", "note": "范围早于任务起点（部署前），没有事件"}
    if through is None or through < max(first, job["start_block"]):
        return {"state": "none", "note": "这个范围还没有采集完成，不能给出结果"}
    if through < last:
        return {
            "state": "partial",
            "covered_through_block": through,
            "note": f"只覆盖到区块 {through}，结果不完整",
        }
    return {"state": "complete"}


def summarize(engine, job: dict, first: int, last: int) -> dict:
    """Totals from done windows only, streamed so memory stays flat."""
    query = (
        sa.select(
            transfers.c.block_number,
            transfers.c.block_time_utc,
            transfers.c.from_address,
            transfers.c.to_address,
            transfers.c.amount_raw,
        )
        .join(window_events, window_events.c.event_id == transfers.c.event_id)
        .join(
            windows,
            (windows.c.job_id == window_events.c.job_id)
            & (windows.c.start_block == window_events.c.start_block),
        )
        .where(
            window_events.c.job_id == job["job_id"],
            windows.c.status == "done",
            transfers.c.block_number.between(first, last),
        )
    )
    events = 0
    amount = minted = burned = 0
    senders, receivers = set(), set()
    first_time = last_time = None
    with engine.connect() as connection:
        rows = connection.execution_options(yield_per=5000).execute(query)
        for _, moment, sender, receiver, raw in rows:
            value = int(raw)
            events += 1
            amount += value
            senders.add(sender)
            receivers.add(receiver)
            if sender == ZERO:
                minted += value
            if receiver == ZERO:
                burned += value
            first_time = moment if first_time is None else min(first_time, moment)
            last_time = moment if last_time is None else max(last_time, moment)
    decimals = job["token_decimals"]

    def tokens(raw: int):
        if decimals is None:
            return None
        whole, fraction = divmod(raw, 10**decimals)
        return (
            f"{whole}.{fraction:0{decimals}d}".rstrip("0").rstrip(".")
            if decimals
            else str(whole)
        )

    return {
        "events": events,
        "amount_raw_sum": str(amount),
        "amount_sum": tokens(amount),
        "minted_raw": str(minted),
        "burned_raw": str(burned),
        "unique_senders": len(senders),
        "unique_receivers": len(receivers),
        "first_event_utc": first_time,
        "last_event_utc": last_time,
    }


def query_job(engine, portal, job_id: str, start_text: str, end_text: str) -> dict:
    start, end = parse_time(start_text), parse_time(end_text)
    first, last = block_range(portal, start, end)
    with engine.connect() as connection:
        job = load_job(connection, job_id)
    result = {
        "job_id": job_id,
        "range": {
            "from": start.isoformat(),
            "to_exclusive": end.isoformat(),
            "blocks": [first, last],
        },
        "coverage": coverage(job, first, last),
    }
    if result["coverage"]["state"] != "none" and last >= first:
        result.update(summarize(engine, job, first, last))
    return result


def report(engine, job_id: str) -> dict:
    """Storage per table and timing of three representative queries."""
    with engine.connect() as connection:
        job = load_job(connection, job_id)
        schema = connection.scalar(sa.text("SELECT DATABASE()"))
        tables = connection.execute(
            sa.text(
                "SELECT table_name, table_rows, data_length, index_length "
                "FROM information_schema.tables WHERE table_schema = :s "
                "ORDER BY data_length + index_length DESC"
            ),
            {"s": schema},
        ).all()
        token = job["token_address"]
        sample = connection.execute(
            sa.select(transfers.c.from_address, transfers.c.block_number)
            .where(transfers.c.chain_id == 56, transfers.c.token_address == token)
            .order_by(transfers.c.block_number.desc())
            .limit(1)
        ).first()
        through = job["verified_through"] or job["start_block"]
        address = sample[0] if sample else ZERO
        probes = {
            "recent_100k_blocks": sa.select(sa.func.count())
            .select_from(transfers)
            .where(
                transfers.c.chain_id == 56,
                transfers.c.token_address == token,
                transfers.c.block_number.between(max(0, through - 100_000), through),
            ),
            "one_address_history": sa.select(sa.func.count())
            .select_from(transfers)
            .where(transfers.c.chain_id == 56, transfers.c.from_address == address),
            "largest_20_amounts": sa.select(transfers.c.amount_raw)
            .where(transfers.c.chain_id == 56, transfers.c.token_address == token)
            .order_by(transfers.c.amount_len.desc(), transfers.c.amount_raw.desc())
            .limit(20),
        }
        timings = {}
        for name, statement in probes.items():
            plan = (
                connection.execute(
                    sa.text(
                        "EXPLAIN "
                        + str(
                            statement.compile(
                                connection, compile_kwargs={"literal_binds": True}
                            )
                        )
                    )
                )
                .mappings()
                .all()
            )
            began = time.perf_counter()
            connection.execute(statement).all()
            timings[name] = {
                "milliseconds": round((time.perf_counter() - began) * 1000, 1),
                "index_used": [row.get("key") for row in plan],
            }
    return {
        "job_id": job_id,
        "tables": [
            {
                "table": name,
                "approx_rows": rows,
                "data_mib": round((data or 0) / 2**20, 2),
                "index_mib": round((index or 0) / 2**20, 2),
            }
            for name, rows, data, index in tables
        ],
        "queries": timings,
    }
