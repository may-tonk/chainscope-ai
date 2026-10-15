"""Create scan jobs and run them window by window, resuming after any stop."""

import json
import re
import sys
import time
import tracemalloc
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy.exc import SQLAlchemyError

from .config import ROOT, Settings
from .database import check_database, engine_for, fail_batch, start_batch
from .dex import attribute_all, discover_pools, load_pools
from .errors import ChainScopeError, DataMismatch, ReconcileMismatch
from .holders import refresh_balances
from .jobs import (
    EVENT_FIELDS,
    begin_window,
    create_job,
    expected_pool_set,
    fail_window,
    fault,
    invalidate_window,
    job_lock,
    job_status,
    load_job,
    next_window,
    plan_windows,
    pool_set_hash,
    record_diffs,
    recover_abandoned,
    reset_stale_pool_windows,
    reset_windows,
    sample_done_windows,
    split_window,
    store_window,
    window_rows,
)
from .parser import ADDRESS, fixed_hex, quantity
from .query import BEIJING, parse_time
from .reconcile import (
    finalized_block,
    reconcile_window,
    sample_receipts,
    sample_swap_receipts,
)
from .rpc_client import RpcClient
from .sqd import (
    BLOCK_HARD_LIMIT,
    DEFAULT_DATASET,
    DEFAULT_PORTAL,
    PARSER_VERSION,
    Portal,
    TooManyEvents,
    window_activity,
)
from .validator import Tally

JOB_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}")
DECIMALS = "0x313ce567"  # decimals()
DEFAULT_WINDOW = 50_000
DEFAULT_EVENT_LIMIT = 50_000
SWAP_FIELDS = (
    "pool_address",
    "block_number",
    "block_time_utc",
    "tx_from",
    "trader",
    "side",
    "token_amount_raw",
    "quote_amount_raw",
    "quote_token",
    "attribution",
    "transfer_matched",
)
FAILURES = (
    ChainScopeError,
    SQLAlchemyError,
    httpx.HTTPError,
    OSError,
    ValueError,
    TypeError,
    KeyError,
)


def say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def require_schema(engine) -> None:
    if not check_database(engine)["schema_ready"]:
        raise ChainScopeError("数据库结构不是最新版本；请先执行 chainscope init-db")


def has_code(rpc: RpcClient, token: str, block: int) -> bool:
    code = rpc.call("eth_getCode", [token, hex(block)])
    if not isinstance(code, str) or not re.fullmatch(r"0x[0-9a-fA-F]*", code):
        raise ChainScopeError("eth_getCode 返回格式错误")
    # 0xef0100 is an EIP-7702 delegation, not a token contract.
    return len(code) > 2 and not code.lower().startswith("0xef0100")


def deploy_block(rpc: RpcClient, token: str, head: int) -> int:
    """First block whose state holds the contract code (binary search)."""
    if has_code(rpc, token, 0):
        return 0
    low, high = 0, head
    while high - low > 1:
        middle = (low + high) // 2
        if has_code(rpc, token, middle):
            high = middle
        else:
            low = middle
    return high


def token_decimals(rpc: RpcClient, token: str, block: int) -> int | None:
    try:
        raw = rpc.call("eth_call", [{"to": token, "data": DECIMALS}, hex(block)])
        return quantity(raw, "decimals", 255)
    except ChainScopeError:
        return None


def block_time(rpc: RpcClient, block: int) -> str:
    header = rpc.call("eth_getBlockByNumber", [hex(block), False])
    if not isinstance(header, dict):
        raise ChainScopeError(f"读取区块 {block} 失败")
    stamp = quantity(header.get("timestamp"), "timestamp")
    return datetime.fromtimestamp(stamp, BEIJING).isoformat()


def create(
    settings: Settings,
    job_id: str,
    token: str,
    *,
    start_block: int | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
    window_size: int = DEFAULT_WINDOW,
    portal: Portal | None = None,
    rpc: RpcClient | None = None,
) -> dict:
    """Create a job. The range may be given as blocks or Beijing times.

    Times are converted once here and shown back; the job keeps block numbers,
    so later runs continue from saved progress and never start over.
    """
    if not JOB_ID.fullmatch(job_id):
        raise ChainScopeError("任务名只能用小写字母开头，包含小写字母、数字、- 和 _")
    if start_block is not None and from_time is not None:
        raise ChainScopeError("--start-block 和 --from 只能二选一")
    token = fixed_hex(token, ADDRESS, "token")
    engine = engine_for(settings)
    notes = []
    try:
        require_schema(engine)
        with ExitStack() as stack:
            if rpc is None:
                rpc = stack.enter_context(RpcClient(settings.require_rpc()))
            if quantity(rpc.call("eth_chainId", []), "chain_id") != 56:
                raise ChainScopeError("RPC 不是 BSC 主网（chain_id=56）")
            head = quantity(rpc.call("eth_blockNumber", []), "head")
            if not has_code(rpc, token, head):
                raise ChainScopeError("该地址在最新区块没有合约代码")
            deployed = deploy_block(rpc, token, head)
            if (from_time or to_time) and portal is None:
                http = stack.enter_context(httpx.Client(timeout=60))
                portal = Portal(DEFAULT_PORTAL, DEFAULT_DATASET, http)
            if from_time is not None:
                moment = parse_time(from_time)
                if moment > datetime.now(UTC):
                    raise ChainScopeError("开始时间晚于现在，没有可采集的区块")
                start_block = portal.block_at(int(moment.timestamp()))
            if start_block is None:
                start_block = deployed
                notes.append("起点为部署区块（eth_getCode 二分查找）")
            elif start_block < deployed:
                notes.append(
                    f"起点早于部署区块 {deployed}，之前没有事件，改从部署区块开始"
                )
                start_block = deployed
            elif start_block > deployed:
                notes.append(
                    f"起点晚于部署区块 {deployed}：之前的事件不会采集，持仓不完整，"
                    "查询这段时间会显示未采集"
                )
            end_block = None
            if to_time is not None:
                moment = parse_time(to_time)
                if moment > datetime.now(UTC):
                    raise ChainScopeError("结束时间晚于现在；要持续同步请省略 --to")
                end_block = portal.block_at(int(moment.timestamp())) - 1
                if end_block < start_block:
                    raise ChainScopeError("结束时间早于起点（或早于部署），范围为空")
            if not 0 <= start_block <= head:
                raise ChainScopeError("起始区块超出链高度")
            decimals = token_decimals(rpc, token, head)
            shown = {"start_block_time_beijing": block_time(rpc, start_block)}
            if end_block is not None:
                shown["end_block_time_beijing"] = block_time(rpc, end_block)
            requests = rpc.requests
        row = create_job(
            engine,
            job_id,
            token,
            source="sqd",
            start_block=start_block,
            window_size=window_size,
            parser_version=PARSER_VERSION,
            decimals=decimals,
            deploy_block=deployed,
            end_block=end_block,
        )
    finally:
        engine.dispose()
    return {
        **{k: v for k, v in row.items() if k != "created_at_utc"},
        **shown,
        "range_rule": "[from, to)：包含开始时刻，不包含结束时刻；北京时间",
        "follows_head": end_block is None,
        "notes": notes,
        "rpc_requests": requests,
    }


def event_row(event: dict, decimals: int | None) -> dict:
    row = dict(event)
    row["amount_raw"] = str(row.pop("amount"))
    row["token_decimals"] = decimals
    return row


def tally_of(events: list[dict]) -> Tally:
    tally = Tally()
    for e in events:
        tally.add(
            e["tx_hash"],
            e["log_index"],
            e["from_address"],
            e["to_address"],
            e["amount"],
            e["block_number"],
        )
    return tally


class Budget:
    """Soft limits: checked before each window and each balance chunk.

    A window or chunk that has started always finishes (or rolls back), so a
    run can pass a limit by up to one window. The summary reports the overrun.
    The hard stop is the scheduled task's execution time limit; anything it
    kills is recovered on the next run.
    """

    def __init__(self, clock, *, seconds=None, requests=None, compute_units=None):
        self._clock = clock
        self._started = clock()
        self.seconds = seconds
        self.requests = requests
        self.compute_units = compute_units

    def elapsed(self) -> float:
        return self._clock() - self._started

    def exhausted(self, portal: Portal, rpc: RpcClient | None) -> str | None:
        used = portal.stats["requests"] + (rpc.requests if rpc else 0)
        if self.seconds is not None and self.elapsed() >= self.seconds:
            return "max_runtime"
        if self.requests is not None and used >= self.requests:
            return "max_requests"
        if (
            self.compute_units is not None
            and rpc is not None
            and rpc.compute_units() >= self.compute_units
        ):
            return "max_compute_units"
        return None


class RunLog:
    """One JSON line per event of interest, to the terminal and a daily file."""

    def __init__(self, notify, path: Path | None):
        self._notify = notify
        self._path = path

    def __call__(self, record: dict) -> None:
        line = json.dumps(
            {"at_utc": datetime.now(UTC).isoformat(timespec="seconds"), **record},
            ensure_ascii=False,
            default=str,
        )
        self._notify(line)
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")


def source_head(portal: Portal, rpc: RpcClient | None, summary: dict) -> int:
    """Finalized height. With an RPC both sources must answer; the lower wins.

    There is no silent fallback: if the RPC cannot give its finalized block the
    run stops before collecting anything. Without an RPC (--no-reconcile) the
    summary says the height is confirmed by SQD alone.
    """
    head = portal.finalized_head()
    summary["portal_finalized_head"] = head
    if rpc is None:
        summary["finality"] = "sqd_only"
        return head
    try:
        rpc_head = finalized_block(rpc)
    except ChainScopeError as exc:
        raise ChainScopeError(
            f"无法读取 RPC 的 finalized 高度（{exc}）。对账模式要求两个来源都确认，"
            "本次不采集；请稍后重试，或明确使用 --no-reconcile（仅 SQD 确认）"
        ) from None
    summary["rpc_finalized_head"] = rpc_head
    summary["finality"] = "min(sqd, rpc)"
    return min(head, rpc_head)


def usage(portal: Portal, rpc: RpcClient | None) -> dict:
    result = {"portal": dict(portal.stats)}
    if rpc is not None:
        result["rpc"] = {
            "requests": rpc.requests,
            "calls": dict(rpc.calls),
            "rate_limited": rpc.rate_limited,
            "estimated_compute_units": rpc.compute_units(),
        }
    return result


def sync_job(
    engine,
    job_id: str,
    portal: Portal,
    *,
    rpc: RpcClient | None = None,
    max_windows: int | None = None,
    max_seconds: float | None = None,
    max_requests: int | None = None,
    max_compute_units: int | None = None,
    event_limit: int = DEFAULT_EVENT_LIMIT,
    block_hard_limit: int = BLOCK_HARD_LIMIT,
    clock=time.monotonic,
    sleep=time.sleep,
    notify=say,
    log_path: Path | None = None,
) -> dict:
    """Collect pending windows in block order until caught up or out of budget.

    With an RPC client every window is reconciled against Alchemy and a few
    receipts before it is committed; without one it is marked 'skipped'.
    Windows whose data disagrees are quarantined and the run continues with
    the next one; transient failures stop the run.
    """
    require_schema(engine)
    log = RunLog(notify, log_path)
    budget = Budget(
        clock,
        seconds=max_seconds,
        requests=max_requests,
        compute_units=max_compute_units,
    )
    summary = {
        "job_id": job_id,
        "parser_version": PARSER_VERSION,
        "windows_done": 0,
        "events": 0,
        "inserted": 0,
        "swaps": 0,
        "splits": 0,
        "quarantined": [],
        "reconcile": "alchemy" if rpc is not None else "skipped",
    }
    with job_lock(engine, job_id):
        with engine.connect() as connection:
            job = load_job(connection, job_id)
            pools = load_pools(connection, job_id)
        if job["status"] != "active":
            raise ChainScopeError(f"任务 {job_id} 已暂停")
        if job["source"] != "sqd":
            raise ChainScopeError(f"暂不支持数据源 {job['source']}")
        token = job["token_address"]
        summary["pools"] = len(pools)
        summary["recovered_abandoned"] = recover_abandoned(engine, job_id)
        summary["pool_windows_reset"] = reset_stale_pool_windows(engine, job_id, pools)
        head = source_head(portal, rpc, summary)
        summary["source_head"] = head
        summary["planned_windows"] = plan_windows(engine, job_id, head)
        while True:
            if max_windows is not None and summary["windows_done"] >= max_windows:
                summary["stopped"] = "max_windows"
                break
            reason = budget.exhausted(portal, rpc)
            if reason:
                summary["stopped"] = reason
                break
            window = next_window(engine, job_id)
            if window is None:
                summary["stopped"] = "caught_up"
                break
            start, end = window["start_block"], window["end_block"]
            attempt = str(uuid4())
            start_batch(
                engine, attempt, token, start, end, None, provider="sqd", job_id=job_id
            )
            begin_window(engine, job_id, start, attempt)
            tick = clock()
            try:
                found, legs = window_activity(
                    portal, token, start, end, event_limit, pools, block_hard_limit
                )
                swap_rows = attribute_all(legs, found)
                fault("after_fetch")
                rows = [event_row(e, job["token_decimals"]) for e in found]
                tally = tally_of(found)
                extra = {
                    "reconcile": "skipped",
                    "receipt_checked": None,
                    "parser_version": PARSER_VERSION,
                    "pool_set": expected_pool_set(pools, end),
                }
                if rpc is not None:
                    diffs = reconcile_window(rpc, token, start, end, found)
                    if diffs:
                        raise ReconcileMismatch(
                            f"窗口 {start}-{end} 与 Alchemy 不一致：{len(diffs)} 处差异",
                            diffs,
                        )
                    extra["reconcile"] = "match"
                    extra["receipt_checked"] = sample_receipts(
                        rpc, rows, start
                    ) + sample_swap_receipts(rpc, swap_rows, pools, start)
                result = store_window(
                    engine,
                    job,
                    window,
                    attempt,
                    rows,
                    tally,
                    head,
                    sleep=sleep,
                    extra=extra,
                    swap_rows=swap_rows,
                )
            except TooManyEvents:
                fail_batch(engine, attempt, "TooManyEvents")
                middle, second = split_window(engine, job_id, start, end)
                summary["splits"] += 1
                log({"split": [start, end], "into": [[start, middle], [second, end]]})
                continue
            except DataMismatch as exc:
                # Evidence first, then quarantine; the run goes on elsewhere.
                record_diffs(engine, job_id, start, end, exc.diffs)
                fail_window(engine, job_id, start, exc.kind)
                fail_batch(engine, attempt, exc.kind)
                summary["quarantined"].append(
                    {"window": [start, end], "error_type": exc.kind, "reason": str(exc)}
                )
                log({"quarantined": [start, end], "error_type": exc.kind})
                continue
            except FAILURES as exc:
                try:
                    fail_window(engine, job_id, start, type(exc).__name__)
                    fail_batch(engine, attempt, type(exc).__name__)
                except SQLAlchemyError:
                    pass  # left 'running'; the next run marks it abandoned
                log({"failed": [start, end], "error_type": type(exc).__name__})
                raise
            summary["windows_done"] += 1
            summary["events"] += tally.events
            summary["inserted"] += result.get("inserted", 0)
            summary["swaps"] += len(swap_rows)
            summary["verified_through"] = result["through"]
            log(
                {
                    "window": [start, end],
                    "events": tally.events,
                    "swaps": len(swap_rows),
                    "inserted": result.get("inserted"),
                    "dropped_links": result.get("dropped_links"),
                    "reconcile": extra["reconcile"],
                    "receipt_checked": extra["receipt_checked"],
                    "seconds": round(clock() - tick, 2),
                    "verified_through": result["through"],
                    "requests": portal.stats["requests"] + (rpc.requests if rpc else 0),
                }
            )
        try:
            summary["balances"] = refresh_balances(
                engine,
                job_id,
                stop=lambda: budget.exhausted(portal, rpc) is not None,
                sleep=sleep,
            )
        except ChainScopeError as exc:
            # Collected windows stay committed; only the balance cache waits.
            summary["balances"] = {"complete": False, "error": str(exc)}
    elapsed = budget.elapsed()
    summary["elapsed_seconds"] = round(elapsed, 1)
    if max_seconds is not None:
        summary["budget_overrun_seconds"] = round(max(0.0, elapsed - max_seconds), 1)
    summary["usage"] = usage(portal, rpc)
    return summary


def compare_rows(stored: dict, fetched: dict, fields, label: str) -> list[dict]:
    """Field-by-field differences between stored rows and a fresh fetch."""
    diffs = []
    for key in stored.keys() | fetched.keys():
        old, new = stored.get(key), fetched.get(key)
        if old is None:
            kind, detail = f"recheck_{label}_new_in_source", {}
        elif new is None:
            kind, detail = f"recheck_{label}_missing_in_source", {}
        else:
            changed = {
                f: {"stored": str(old[f]), "fetched": str(new[f])}
                for f in fields
                if old[f] != new[f]
            }
            if not changed:
                continue
            kind, detail = f"recheck_{label}_changed", {"fields": changed}
        diffs.append(
            {"kind": kind, "tx_hash": key[0], "log_index": key[1], "detail": detail}
        )
    return diffs


def recheck_job(
    engine,
    job_id: str,
    portal: Portal,
    *,
    rpc: RpcClient | None = None,
    sample: int = 20,
    seed=None,
    notify=say,
) -> dict:
    """Fetch random done windows again and compare every stored field.

    Transfers: all fields incl. block number, hash and time. Swaps: all fields,
    when the window was collected with the pools enabled now. With an RPC,
    Alchemy is compared too. A changed window is quarantined and the
    watermark rewound; `redo` releases it after investigation.
    """
    require_schema(engine)
    checked, changed = [], []
    swaps_checked = swaps_skipped = 0
    with job_lock(engine, job_id):
        with engine.connect() as connection:
            job = load_job(connection, job_id)
            pools = load_pools(connection, job_id)
        token = job["token_address"]
        for window in sample_done_windows(engine, job_id, sample, seed):
            start, end = window["start_block"], window["end_block"]
            current = (window["pool_set"] or pool_set_hash(())) == expected_pool_set(
                pools, end
            )
            found, legs = window_activity(
                portal, token, start, end, 10**9, pools if current else None, 10**9
            )
            fresh = {
                (e["tx_hash"], e["log_index"]): event_row(e, job["token_decimals"])
                for e in found
            }
            stored_events, stored_swaps = window_rows(engine, job_id, start, end)
            diffs = compare_rows(stored_events, fresh, EVENT_FIELDS, "transfer")
            if current:
                fresh_swaps = {
                    (s["tx_hash"], s["log_index"]): s
                    for s in attribute_all(legs, found)
                }
                diffs += compare_rows(stored_swaps, fresh_swaps, SWAP_FIELDS, "swap")
                swaps_checked += 1
            else:
                swaps_skipped += 1
            if rpc is not None:
                diffs += reconcile_window(rpc, token, start, end, found)
            checked.append([start, end])
            if diffs:
                record_diffs(engine, job_id, start, end, diffs)
                invalidate_window(engine, job_id, start, "RecheckChanged")
                changed.append({"window": [start, end], "diffs": len(diffs)})
                notify(
                    f"窗口 {start}-{end} 复查发现 {len(diffs)} 处差异，已隔离并回退水位"
                )
    return {
        "job_id": job_id,
        "windows_checked": len(checked),
        "checked": {
            "transfers": "逐字段（含区块号、区块哈希、时间）",
            "swaps": f"{swaps_checked} 个窗口逐字段"
            + (
                f"；{swaps_skipped} 个窗口池子集合已变化，未比对，sync 会重做"
                if swaps_skipped
                else ""
            ),
            "alchemy": rpc is not None,
        },
        "changed_windows": changed,
        "ok": not changed,
        "usage": usage(portal, rpc),
    }


def redo_job(engine, job_id: str, *, first=None, last=None, below_version=None):
    """Reset windows: below a parser version (default: the current one), or a range.

    A range also releases quarantined windows in it.
    """
    require_schema(engine)
    if first is None and last is None and below_version is None:
        below_version = PARSER_VERSION
    with job_lock(engine, job_id):
        count = reset_windows(
            engine, job_id, below_version=below_version, first=first, last=last
        )
    return {"job_id": job_id, "windows_reset": count}


def open_sources(settings: Settings, stack: ExitStack, *, reconcile: bool):
    http = stack.enter_context(
        httpx.Client(timeout=httpx.Timeout(120, connect=15), follow_redirects=False)
    )
    portal = Portal(DEFAULT_PORTAL, DEFAULT_DATASET, http, notify=say)
    rpc = stack.enter_context(RpcClient(settings.require_rpc())) if reconcile else None
    return portal, rpc


def run_sync(
    settings: Settings,
    job_id: str,
    *,
    reconcile: bool = True,
    trace_memory: bool = False,
    **limits,
) -> dict:
    engine = engine_for(settings)
    if trace_memory:
        tracemalloc.start()
    try:
        with ExitStack() as stack:
            portal, rpc = open_sources(settings, stack, reconcile=reconcile)
            log_path = ROOT / "logs" / f"sync-{datetime.now(UTC):%Y%m%d}.jsonl"
            summary = sync_job(
                engine, job_id, portal, rpc=rpc, log_path=log_path, **limits
            )
        if trace_memory:
            summary["memory_peak_mib"] = round(
                tracemalloc.get_traced_memory()[1] / 2**20, 1
            )
        return summary
    finally:
        if trace_memory:
            tracemalloc.stop()
        engine.dispose()


def run_recheck(settings: Settings, job_id: str, *, reconcile=True, **options):
    engine = engine_for(settings)
    try:
        with ExitStack() as stack:
            portal, rpc = open_sources(settings, stack, reconcile=reconcile)
            return recheck_job(engine, job_id, portal, rpc=rpc, **options)
    finally:
        engine.dispose()


def pools_discover(engine, job_id: str, portal: Portal) -> dict:
    """Find PancakeSwap pools of the job's token and schedule their backfill.

    New pools and the windows they invalidate are saved in one transaction;
    running discover again is safe, and sync redoes any window whose pool set
    is out of date whatever happened in between.
    """
    require_schema(engine)
    with job_lock(engine, job_id):
        with engine.connect() as connection:
            job = load_job(connection, job_id)
        head = portal.finalized_head()
        result = discover_pools(engine, portal, job, head)
    return {
        "job_id": job_id,
        "scanned_through": head,
        "added": [
            {k: v for k, v in p.items() if k not in ("job_id", "added_at_utc")}
            for p in result["added"]
        ],
        "windows_reset": result["windows_reset"],
        "note": "报价币不在 WBNB/USDT/BUSD/USDC 内的池子默认停用；确认后用 pools enable 启用",
    }


def status_with_head(
    engine, job_id: str, portal: Portal, *, max_lag_blocks: int | None = None
) -> dict:
    """Job status measured against the Portal's finalized head read just now.

    If the head cannot be read the lag is unknown, never computed from an old
    height; `behind` is then "unknown" when a limit was given.
    """
    head = error = None
    observed = datetime.now(UTC).replace(tzinfo=None)
    try:
        head = portal.finalized_head()
        observed = datetime.now(UTC).replace(tzinfo=None)
    except (ChainScopeError, httpx.HTTPError) as exc:
        error = str(exc)[:300]
    return job_status(
        engine,
        job_id,
        max_lag_blocks=max_lag_blocks,
        head=head,
        head_observed_at=observed if head is not None else None,
        head_error=error,
    )
