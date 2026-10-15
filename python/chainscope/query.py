"""Date-range queries over collected events, honest about partial coverage.

Times are read as Beijing time (UTC+8) unless they carry an offset. A range is
[start, end): the start instant is included and the end instant is not. Block
timestamps are whole seconds and several BSC blocks can share one, so a time is
whole seconds too: fractional seconds are rejected rather than rounded.

Only blocks up to the contiguous watermark are counted, for Transfers and
Swaps alike, so totals never include a window that is pending, failed or
waiting to be redone. The coverage block says exactly which blocks were counted.
"""

import re
import time
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from .balances import range_params
from .errors import ChainScopeError
from .jobs import load_job
from .models import swaps, transfers, window_events

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
    if moment.microsecond:
        raise ChainScopeError(f"不支持小数秒：{text}（区块时间戳精确到秒，请写到整秒）")
    if DATE.fullmatch(text) or moment.tzinfo is None:
        moment = moment.replace(tzinfo=BEIJING)
    return moment


def block_range(portal, start: datetime, end: datetime) -> tuple[int, int]:
    """Blocks with timestamps in [start, end), all blocks of a shared second included."""
    if end <= start:
        raise ChainScopeError("结束时间必须晚于开始时间")
    first = portal.block_at(int(start.timestamp()))
    after = portal.block_at(int(end.timestamp()))
    return first, after - 1


def coverage(job: dict, first: int, last: int) -> dict:
    """Split [first, last] by what is known about each part.

    before_deploy: the contract had no code yet (deploy block found from
    eth_getCode), so there cannot be events. not_collected: after deployment
    but before the job's start block. verified: done and under the watermark.
    not_verified: not collected yet, or waiting to be redone. outside_job: after
    the job's fixed end block. Only the verified part is counted.
    """
    deploy = job.get("deploy_block")
    start = job["start_block"]
    through = job["verified_through"]
    end_block = job.get("end_block")
    parts = []

    def part(kind, a, b):
        if a <= b:
            parts.append({"kind": kind, "blocks": [a, b]})

    cursor = first
    if deploy is not None:
        part("before_deploy", cursor, min(last, deploy - 1))
        cursor = max(cursor, deploy)
    part("not_collected", cursor, min(last, start - 1))
    cursor = max(cursor, start)
    if through is not None:
        part("verified", cursor, min(last, through))
        cursor = max(cursor, through + 1)
    if end_block is not None:
        part("not_verified", cursor, min(last, end_block))
        part("outside_job", max(cursor, end_block + 1), last)
    else:
        part("not_verified", cursor, last)
    verified = next((p["blocks"] for p in parts if p["kind"] == "verified"), None)
    known = {"before_deploy", "verified"}
    if all(p["kind"] in known for p in parts):
        state = "complete"
    elif verified is None:
        state = "none"
    else:
        state = "partial"
    result = {"state": state, "counted_blocks": verified, "parts": parts}
    if state == "partial":
        result["note"] = f"只统计了区块 {verified[0]}–{verified[1]}，其余部分见 parts"
    elif state == "none":
        result["note"] = "这个范围没有已核实的数据，不能给出结果"
    elif verified is None:
        result["note"] = "整个范围早于合约部署（eth_getCode 确认），没有事件"
    if deploy is None and first < start:
        result["deploy_block_unknown"] = True
    return result


def tokens(raw: int, decimals) -> str | None:
    if decimals is None:
        return None
    sign = "-" if raw < 0 else ""
    whole, fraction = divmod(abs(raw), 10**decimals)
    text = (
        f"{whole}.{fraction:0{decimals}d}".rstrip("0").rstrip(".")
        if decimals
        else str(whole)
    )
    return sign + text


def _linked(job: dict, first: int, last: int):
    """Transfers linked to the job's windows with blocks in [first, last]."""
    params = range_params(job, first, last)
    return (
        sa.select(transfers)
        .join(window_events, window_events.c.event_id == transfers.c.event_id)
        .where(
            window_events.c.job_id == job["job_id"],
            window_events.c.start_block.between(params["window_first"], last),
            transfers.c.block_number.between(first, last),
        )
    )


def summarize(engine, job: dict, first: int, last: int) -> dict:
    """Exact totals, streamed; distinct counts are left to MySQL.

    Amounts are up to 78 digits and their sums longer still, so they are added
    as Python integers row by row; nothing is cast to DECIMAL(65,0), which
    would clamp. Distinct addresses are counted in SQL so memory stays flat
    however many addresses there are.
    """
    linked = _linked(job, first, last).subquery()
    events = 0
    amount = minted = burned = 0
    with engine.connect() as connection:
        rows = connection.execution_options(yield_per=5000).execute(
            sa.select(linked.c.from_address, linked.c.to_address, linked.c.amount_raw)
        )
        for sender, receiver, raw in rows:
            value = int(raw)
            events += 1
            amount += value
            if sender == ZERO:
                minted += value
            if receiver == ZERO:
                burned += value
        senders, receivers, first_time, last_time = connection.execute(
            sa.select(
                sa.func.count(sa.distinct(linked.c.from_address)),
                sa.func.count(sa.distinct(linked.c.to_address)),
                sa.func.min(linked.c.block_time_utc),
                sa.func.max(linked.c.block_time_utc),
            )
        ).one()
    decimals = job["token_decimals"]
    return {
        "events": events,
        "amount_raw_sum": str(amount),
        "amount_sum": tokens(amount, decimals),
        "minted_raw": str(minted),
        "burned_raw": str(burned),
        "unique_senders": senders,
        "unique_receivers": receivers,
        "first_event_utc": first_time,
        "last_event_utc": last_time,
    }


def swap_summary(engine, job: dict, first: int, last: int) -> dict:
    """Buys and sells of tracked pools in [first, last], per side and quote token.

    The caller passes the counted (verified) range. Sums are exact Python
    integers over streamed rows; distinct traders are counted in SQL.
    """
    where = [swaps.c.job_id == job["job_id"], swaps.c.block_number.between(first, last)]
    groups = {}
    attribution = {}
    with engine.connect() as connection:
        rows = connection.execution_options(yield_per=5000).execute(
            sa.select(
                swaps.c.side,
                swaps.c.quote_token,
                swaps.c.token_amount_raw,
                swaps.c.quote_amount_raw,
                swaps.c.attribution,
            ).where(*where)
        )
        for side, quote_token, token_raw, quote_raw, how in rows:
            group = groups.setdefault((side, quote_token), [0, 0, 0])
            group[0] += 1
            group[1] += int(token_raw)
            group[2] += int(quote_raw)
            attribution[how] = attribution.get(how, 0) + 1
        distinct = dict(
            ((side, quote_token), n)
            for side, quote_token, n in connection.execute(
                sa.select(
                    swaps.c.side,
                    swaps.c.quote_token,
                    sa.func.count(sa.distinct(swaps.c.trader)),
                )
                .where(*where)
                .group_by(swaps.c.side, swaps.c.quote_token)
            )
        )
        traders = connection.scalar(
            sa.select(sa.func.count(sa.distinct(swaps.c.trader))).where(*where)
        )
    return {
        "by_side": [
            {
                "side": side,
                "quote_token": quote_token,
                "swaps": n,
                "token_amount_raw": str(token_sum),
                "quote_amount_raw": str(quote_sum),
                "traders": distinct.get((side, quote_token), 0),
            }
            for (side, quote_token), (n, token_sum, quote_sum) in sorted(groups.items())
        ],
        "traders": traders,
        "attribution": attribution,
        "note": "trader 是交易发起地址；router/ambiguous 表示经聚合器或合约转手",
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
    counted = result["coverage"]["counted_blocks"]
    if counted is not None:
        result.update(summarize(engine, job, *counted))
        result["swaps"] = swap_summary(engine, job, *counted)
    elif result["coverage"]["state"] == "complete":
        result.update(summarize(engine, job, 1, 0))  # provably empty
        result["swaps"] = swap_summary(engine, job, 1, 0)
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
