"""Bounded, read-only acceptance of coverage, block logs and cached balances."""

import random
import time
from collections import Counter
from datetime import UTC, datetime
from fractions import Fraction
from zoneinfo import ZoneInfo

import httpx
import sqlalchemy as sa

from .balances import ZERO
from .errors import ChainScopeError
from .holders import BALANCE_OF
from .jobs import load_job
from .models import (
    balances,
    block_headers,
    pool_scans,
    supplement_jobs,
    swap_windows,
    transfers,
    window_events,
    windows,
)
from .parser import ADDRESS, HASH, fixed_hex, quantity
from .validator import TRANSFER_TOPIC

MAX_BLOCKS = 8
EVENT_FIELDS = (
    "block_number",
    "block_hash",
    "block_time_utc",
    "tx_hash",
    "log_index",
    "from_address",
    "to_address",
    "amount_raw",
)


def coverage(rows, first, last):
    """Inspect small window tables, never infer coverage from event counts."""
    expected, problems, counts = first, [], Counter()
    events = 0
    for row in sorted(rows, key=lambda r: r["start_block"]):
        start, end = row["start_block"], row["end_block"]
        counts[row["status"]] += 1
        if start != expected:
            problems.append(
                {
                    "kind": "gap" if start > expected else "overlap",
                    "expected_start": expected,
                    "actual_start": start,
                }
            )
        if end < start or start < first or end > last:
            problems.append({"kind": "range", "start": start, "end": end})
        if row["status"] != "done":
            problems.append(
                {"kind": "unfinished", "start": start, "status": row["status"]}
            )
        if row["event_count"] is None:
            problems.append({"kind": "missing_count", "start": start})
        else:
            events += int(row["event_count"])
        expected = max(expected, end + 1)
    if expected != last + 1:
        problems.append(
            {"kind": "end", "expected_end": last, "actual_end": expected - 1}
        )
    return {
        "ok": not problems,
        "windows": len(rows),
        "statuses": dict(counts),
        "committed_event_count": events,
        "problems": problems,
    }


def missing_blocks(numbers, first, last, limit=2):
    """Find empty blocks using only grouped rows from selected windows."""
    found, next_block = [], first
    for number in sorted(set(numbers)):
        while next_block < number and len(found) < limit:
            found.append(next_block)
            next_block += 1
        next_block = max(next_block, number + 1)
        if len(found) == limit:
            return found
    while next_block <= last and len(found) < limit:
        found.append(next_block)
        next_block += 1
    return found


def _block_rows(c, job, number):
    return list(
        c.execute(
            sa.select(transfers)
            .join(window_events, window_events.c.event_id == transfers.c.event_id)
            .where(
                window_events.c.job_id == job["job_id"],
                transfers.c.chain_id == job["chain_id"],
                transfers.c.token_address == job["token_address"],
                transfers.c.block_number == number,
            )
        ).mappings()
    )


def select_blocks(c, job, done, cutoff):
    """Spread samples through history; dense/empty refer to inspected windows."""
    selected = {}

    def add(number, reason):
        selected.setdefault(number, []).append(reason)

    add(job["start_block"], "range_start")
    add(cutoff, "range_end")
    positive = [r for r in done if r["event_count"]]
    if positive:
        candidates = [
            (positive[0], "early"),
            (positive[len(positive) // 2], "middle"),
            (positive[-1], "late"),
            (
                max(
                    positive,
                    key=lambda r: Fraction(
                        r["event_count"], r["end_block"] - r["start_block"] + 1
                    ),
                ),
                "dense_window",
            ),
        ]
        grouped = {}
        for window, reason in candidates:
            start, end = window["start_block"], window["end_block"]
            if start not in grouped:
                grouped[start] = c.execute(
                    sa.select(transfers.c.block_number, sa.func.count().label("events"))
                    .join(
                        window_events, window_events.c.event_id == transfers.c.event_id
                    )
                    .where(
                        window_events.c.job_id == job["job_id"],
                        window_events.c.start_block == start,
                        transfers.c.block_number.between(start, end),
                    )
                    .group_by(transfers.c.block_number)
                    .order_by(transfers.c.block_number)
                ).all()
            rows = grouped[start]
            if rows:
                chosen = (
                    max(rows, key=lambda r: r.events)
                    if reason == "dense_window"
                    else rows[len(rows) // 2]
                )
                add(chosen.block_number, reason)
        for window, _ in candidates:
            for number in missing_blocks(
                [r.block_number for r in grouped[window["start_block"]]],
                window["start_block"],
                window["end_block"],
            ):
                if len(selected) < MAX_BLOCKS:
                    add(number, "locally_empty")
    return [{"block_number": n, "reasons": reasons} for n, reasons in selected.items()][
        :MAX_BLOCKS
    ]


def select_holders(c, job, top, random_count, seed):
    """Reservoir sampling avoids ORDER BY RAND and retains exact integers."""
    where = (
        balances.c.chain_id == job["chain_id"],
        balances.c.token_address == job["token_address"],
        balances.c.address != ZERO,
        balances.c.balance_raw > 0,
    )
    query = sa.select(balances.c.address, balances.c.balance_raw).where(*where)
    ranked = (
        c.execute(
            query.order_by(balances.c.balance_raw.desc(), balances.c.address).limit(top)
        ).all()
        if top
        else []
    )
    excluded = {r.address for r in ranked}
    reservoir, seen, rng = [], 0, random.Random(seed)
    if random_count:
        with c.execute(
            query.order_by(balances.c.address).execution_options(
                stream_results=True, yield_per=1000
            )
        ) as result:
            for row in result:
                if row.address in excluded:
                    continue
                seen += 1
                if len(reservoir) < random_count:
                    reservoir.append(row)
                else:
                    position = rng.randrange(seen)
                    if position < random_count:
                        reservoir[position] = row
    return [
        {
            "address": r.address,
            "balance_raw": str(int(r.balance_raw)),
            "selection": kind,
        }
        for kind, rows in (("top", ranked), ("random", reservoir))
        for r in rows
    ]


def canonical_events(raw_logs, header, token):
    """Validate a complete filtered eth_getLogs result before comparing it."""
    if not isinstance(raw_logs, list):
        raise ChainScopeError("区块日志响应不是列表")
    number = quantity(header.get("number"), "block.number")
    block_hash = fixed_hex(header.get("hash"), HASH, "block.hash")
    moment = datetime.fromtimestamp(
        quantity(header.get("timestamp"), "timestamp"), UTC
    ).replace(tzinfo=None)
    rows, seen = [], set()
    for log in raw_logs:
        if not isinstance(log, dict) or log.get("removed", False):
            raise ChainScopeError("日志格式错误或已经被移除")
        topics = log.get("topics")
        if (
            quantity(log.get("blockNumber"), "log.block") != number
            or fixed_hex(log.get("blockHash"), HASH, "log.block_hash") != block_hash
            or fixed_hex(log.get("address"), ADDRESS, "log.address") != token
            or not isinstance(topics, list)
            or len(topics) != 3
            or fixed_hex(topics[0], HASH, "topic0") != TRANSFER_TOPIC
        ):
            raise ChainScopeError("日志区块、合约或事件类型与请求不符")
        addresses = []
        for topic in topics[1:]:
            value = fixed_hex(topic, HASH, "indexed address")
            if value[2:26] != "0" * 24:
                raise ChainScopeError("日志地址填充不符合标准 ERC-20")
            addresses.append("0x" + value[-40:])
        index = quantity(log.get("logIndex"), "log.index", 2**32 - 1)
        if index in seen:
            raise ChainScopeError("同一区块日志序号重复")
        seen.add(index)
        rows.append(
            dict(
                block_number=number,
                block_hash=block_hash,
                block_time_utc=moment,
                tx_hash=fixed_hex(log.get("transactionHash"), HASH, "log.tx"),
                log_index=index,
                from_address=addresses[0],
                to_address=addresses[1],
                amount_raw=str(
                    quantity(fixed_hex(log.get("data"), HASH, "log.data"), "amount")
                ),
            )
        )
    return rows


def compare_events(local, chain):
    def keyed(rows):
        result = {}
        for row in rows:
            key = (row["tx_hash"], row["log_index"])
            if key in result:
                raise ChainScopeError("比较数据中存在重复事件身份")
            result[key] = row
        return result

    left, right = keyed(local), keyed(chain)
    missing, extra, changed = [], [], []
    for key in sorted(right.keys() - left.keys()):
        missing.append({"tx_hash": key[0], "log_index": key[1]})
    for key in sorted(left.keys() - right.keys()):
        extra.append({"tx_hash": key[0], "log_index": key[1]})
    for key in sorted(left.keys() & right.keys()):
        fields = [f for f in EVENT_FIELDS if left[key][f] != right[key][f]]
        if fields:
            changed.append({"tx_hash": key[0], "log_index": key[1], "fields": fields})
    return {
        "ok": not missing and not extra and not changed,
        "local_events": len(local),
        "chain_events": len(chain),
        "missing_in_database": missing,
        "extra_in_database": extra,
        "changed": changed,
    }


def _check_block(c, job, rpc, sample):
    number = sample["block_number"]
    header = rpc.call("eth_getBlockByNumber", [hex(number), False])
    if (
        not isinstance(header, dict)
        or quantity(header.get("number"), "block") != number
    ):
        raise ChainScopeError("抽查区块头缺失或高度不符")
    logs = rpc.call(
        "eth_getLogs",
        [
            {
                "fromBlock": hex(number),
                "toBlock": hex(number),
                "address": job["token_address"],
                "topics": [TRANSFER_TOPIC],
            }
        ],
    )
    chain = canonical_events(logs, header, job["token_address"])
    local = _block_rows(c, job, number)
    result = compare_events(local, chain)
    stored_header = (
        c.execute(
            sa.select(block_headers).where(
                block_headers.c.chain_id == job["chain_id"],
                block_headers.c.block_number == number,
            )
        )
        .mappings()
        .first()
    )
    # Even empty blocks need a header comparison when one was cached.
    header_ok = (
        None
        if stored_header is None
        else (
            stored_header["block_hash"] == fixed_hex(header.get("hash"), HASH, "hash")
            and stored_header["block_time_utc"]
            == datetime.fromtimestamp(
                quantity(header.get("timestamp"), "time"), UTC
            ).replace(tzinfo=None)
        )
    )
    return {
        **sample,
        **result,
        "header_cache_present": stored_header is not None,
        "header_matches_cache": header_ok,
        "ok": result["ok"] and header_ok is not False,
        "status": "checked",
    }


def accept_sample(engine, rpc, job_id, cutoff, *, top=5, random_count=5, seed=0):
    """No locks, writes, refresh, repair, migration or historical re-download.

    All local reads share one MySQL snapshot enforced READ ONLY by the server.
    External failures are inconclusive, never silently converted to empty logs.
    """
    if (
        cutoff < 0
        or not 0 <= top <= 20
        or not 0 <= random_count <= 20
        or top + random_count == 0
    ):
        raise ChainScopeError(
            "截止区块及抽查数量无效；持仓每组限 0 到 20，至少抽查一个"
        )
    started = time.monotonic()
    report = {
        "job_id": job_id,
        "cutoff_block": cutoff,
        "seed": seed,
        "scope": "全部窗口连续性；最多8个区块的目标代币Transfer集合；部分持仓余额",
        "read_only": True,
        "source": "existing_alchemy_rpc",
        "full_history_source_verified": False,
        "started_at_beijing": datetime.now(UTC)
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .isoformat(),
    }
    with engine.connect() as c:
        c.exec_driver_sql("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        job = load_job(c, job_id)
        if job["chain_id"] != 56 or cutoff < job["start_block"]:
            raise ChainScopeError("仅支持 BSC 主网及任务起点之后的固定范围")
        # Cached balances represent precisely one mark, so require the full
        # current completed range rather than quietly comparing different dates.
        if job["verified_through"] != cutoff or job["balances_through"] != cutoff:
            raise ChainScopeError(
                "截止区块必须等于当前正式入库及持仓缓存进度；不会刷新缓存"
            )
        report.update(
            start_block=job["start_block"], token_address=job["token_address"]
        )
        transfer_windows = list(
            c.execute(
                sa.select(windows)
                .where(windows.c.job_id == job_id)
                .order_by(windows.c.start_block)
            ).mappings()
        )
        swap_rows = list(
            c.execute(
                sa.select(swap_windows)
                .where(swap_windows.c.job_id == job_id)
                .order_by(swap_windows.c.start_block)
            ).mappings()
        )
        pool_mark = c.scalar(
            sa.select(pool_scans.c.scanned_through).where(pool_scans.c.job_id == job_id)
        )
        swap_mark = c.scalar(
            sa.select(supplement_jobs.c.verified_through).where(
                supplement_jobs.c.job_id == job_id
            )
        )
        report["coverage"] = {
            "transfers": coverage(transfer_windows, job["start_block"], cutoff),
            "swaps": coverage(swap_rows, job["start_block"], cutoff),
            "pool_discovery_through": pool_mark,
            "swaps_through": swap_mark,
            "balances_through": job["balances_through"],
        }
        coverage_ok = (
            report["coverage"]["transfers"]["ok"]
            and report["coverage"]["swaps"]["ok"]
            and pool_mark is not None
            and pool_mark >= cutoff
            and swap_mark is not None
            and swap_mark >= cutoff
        )
        if not coverage_ok:
            report.update(ok=False, status="coverage_failed", blocks=[], holders=[])
        else:
            if quantity(rpc.call("eth_chainId", []), "chain_id") != job["chain_id"]:
                raise ChainScopeError("RPC 链 ID 与任务不一致")
            samples = select_blocks(c, job, transfer_windows, cutoff)
            holders = select_holders(c, job, top, random_count, seed)
            source_available = True
            report.update(
                blocks=[],
                holders=[],
                planned_blocks=samples,
                requested_holders=top + random_count,
            )
            for sample in samples:
                if not source_available:
                    report["blocks"].append(
                        {**sample, "ok": False, "status": "not_checked"}
                    )
                    continue
                try:
                    result = _check_block(c, job, rpc, sample)
                except (ChainScopeError, httpx.HTTPError) as exc:
                    source_available = False
                    result = {
                        **sample,
                        "ok": False,
                        "status": "inconclusive",
                        "error": str(exc)
                        if isinstance(exc, ChainScopeError)
                        else type(exc).__name__,
                    }
                report["blocks"].append(result)
            for holder in holders:
                if not source_available:
                    report["holders"].append(
                        {**holder, "ok": False, "status": "not_checked"}
                    )
                    continue
                try:
                    actual = quantity(
                        rpc.call(
                            "eth_call",
                            [
                                {
                                    "to": job["token_address"],
                                    "data": BALANCE_OF
                                    + "0" * 24
                                    + holder["address"][2:],
                                },
                                hex(cutoff),
                            ],
                        ),
                        "balanceOf",
                    )
                    result = {
                        **holder,
                        "chain_balance_raw": str(actual),
                        "ok": actual == int(holder["balance_raw"]),
                        "status": "checked",
                    }
                except (ChainScopeError, httpx.HTTPError) as exc:
                    source_available = False
                    result = {
                        **holder,
                        "ok": False,
                        "status": "inconclusive",
                        "error": str(exc)
                        if isinstance(exc, ChainScopeError)
                        else type(exc).__name__,
                    }
                report["holders"].append(result)
            results = report["blocks"] + report["holders"]
            report["ok"] = (
                len(holders) == top + random_count
                and bool(results)
                and all(r["ok"] for r in results)
            )
            report["status"] = "sample_passed" if report["ok"] else "needs_attention"
    report.update(
        elapsed_seconds=round(time.monotonic() - started, 2),
        rpc_requests=rpc.requests,
        rpc_calls=dict(rpc.calls),
        limitations=[
            "抽查通过不代表全历史无漏项，也不代表所有DEX已覆盖",
            "同一家服务的日志和余额对照不属于独立供应商验证",
            "密集区块从选定窗口中选取，不是扫描全库得出的全局最密集区块",
            "持仓只抽取缓存中正余额地址，不覆盖全部地址或代币特殊机制",
        ],
    )
    return report
