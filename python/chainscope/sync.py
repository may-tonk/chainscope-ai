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
from .errors import ChainScopeError
from .jobs import (
    begin_window,
    create_job,
    fail_window,
    fault,
    invalidate_window,
    job_lock,
    load_job,
    next_window,
    plan_windows,
    record_diffs,
    recover_abandoned,
    reset_windows,
    sample_done_windows,
    split_window,
    store_window,
    window_tally,
)
from .parser import ADDRESS, fixed_hex, quantity
from .reconcile import finalized_block, reconcile_window, sample_receipts
from .rpc_client import RpcClient
from .sqd import (
    DEFAULT_DATASET,
    DEFAULT_PORTAL,
    PARSER_VERSION,
    Portal,
    TooManyEvents,
    window_transfers,
)
from .validator import Tally

JOB_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}")
DECIMALS = "0x313ce567"  # decimals()
DEFAULT_WINDOW = 50_000
DEFAULT_EVENT_LIMIT = 50_000
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


def create(
    settings: Settings,
    job_id: str,
    token: str,
    *,
    start_block: int | None = None,
    window_size: int = DEFAULT_WINDOW,
) -> dict:
    if not JOB_ID.fullmatch(job_id):
        raise ChainScopeError("任务名只能用小写字母开头，包含小写字母、数字、- 和 _")
    token = fixed_hex(token, ADDRESS, "token")
    engine = engine_for(settings)
    try:
        require_schema(engine)
        with RpcClient(settings.require_rpc()) as rpc:
            if quantity(rpc.call("eth_chainId", []), "chain_id") != 56:
                raise ChainScopeError("RPC 不是 BSC 主网（chain_id=56）")
            head = quantity(rpc.call("eth_blockNumber", []), "head")
            if not has_code(rpc, token, head):
                raise ChainScopeError("该地址在最新区块没有合约代码")
            if start_block is None:
                start_block = deploy_block(rpc, token, head)
                found_by = "eth_getCode 二分查找部署区块"
            else:
                found_by = "手工指定"
            if not 0 <= start_block <= head:
                raise ChainScopeError("起始区块超出链高度")
            decimals = token_decimals(rpc, token, head)
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
        )
    finally:
        engine.dispose()
    return {
        **{k: v for k, v in row.items() if k != "created_at_utc"},
        "start_block_from": found_by,
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
        )
    return tally


class Budget:
    """Stop between windows when time, request or compute-unit limits are reached."""

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
    """Finalized height of the Portal, capped by the RPC's finalized tag if any."""
    head = portal.finalized_head()
    summary["portal_finalized_head"] = head
    if rpc is not None:
        try:
            rpc_head = finalized_block(rpc)
            summary["rpc_finalized_head"] = rpc_head
            head = min(head, rpc_head)
        except ChainScopeError as exc:
            summary["rpc_finalized_head"] = f"不可用：{exc}"
    return head


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
    clock=time.monotonic,
    sleep=time.sleep,
    notify=say,
    log_path: Path | None = None,
) -> dict:
    """Collect pending windows in block order until caught up or out of budget.

    With an RPC client every window is reconciled against Alchemy and a few
    receipts before it is committed; without one it is marked 'skipped'.
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
        "windows_done": 0,
        "events": 0,
        "inserted": 0,
        "splits": 0,
        "reconcile": "alchemy" if rpc is not None else "skipped",
    }
    with job_lock(engine, job_id):
        with engine.connect() as connection:
            job = load_job(connection, job_id)
        if job["status"] != "active":
            raise ChainScopeError(f"任务 {job_id} 已暂停")
        if job["source"] != "sqd":
            raise ChainScopeError(f"暂不支持数据源 {job['source']}")
        token = job["token_address"]
        summary["recovered_abandoned"] = recover_abandoned(engine, job_id)
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
                found = window_transfers(portal, token, start, end, event_limit)
                fault("after_fetch")
                rows = [event_row(e, job["token_decimals"]) for e in found]
                tally = tally_of(found)
                extra = {"reconcile": "skipped", "receipt_checked": None}
                if rpc is not None:
                    diffs = reconcile_window(rpc, token, start, end, found)
                    if diffs:
                        record_diffs(engine, job_id, start, end, diffs)
                        raise ChainScopeError(
                            f"窗口 {start}-{end} 与 Alchemy 不一致：{len(diffs)} 处差异，"
                            "已记录到 window_diffs；水位不越过此窗口"
                        )
                    extra = {
                        "reconcile": "match",
                        "receipt_checked": sample_receipts(rpc, rows, start),
                    }
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
                )
                stored = window_tally(engine, job_id, start)
                if not stored.matches(
                    tally.events, tally.amount, tally.fingerprint_bytes()
                ):
                    raise ChainScopeError(
                        f"窗口 {start}-{end} 入库后读回与采集结果不一致"
                    )
            except TooManyEvents:
                fail_batch(engine, attempt, "TooManyEvents")
                middle, second = split_window(engine, job_id, start, end)
                summary["splits"] += 1
                log({"split": [start, end], "into": [[start, middle], [second, end]]})
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
            summary["verified_through"] = result["through"]
            log(
                {
                    "window": [start, end],
                    "events": tally.events,
                    "inserted": result.get("inserted"),
                    "reconcile": extra["reconcile"],
                    "receipt_checked": extra["receipt_checked"],
                    "seconds": round(clock() - tick, 2),
                    "verified_through": result["through"],
                    "requests": portal.stats["requests"] + (rpc.requests if rpc else 0),
                }
            )
    summary["elapsed_seconds"] = round(budget.elapsed(), 1)
    summary["usage"] = usage(portal, rpc)
    return summary


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
    """Re-fetch random done windows; a changed window is failed and redone later."""
    require_schema(engine)
    checked, changed = [], []
    with job_lock(engine, job_id):
        with engine.connect() as connection:
            job = load_job(connection, job_id)
        token = job["token_address"]
        for window in sample_done_windows(engine, job_id, sample, seed):
            start, end = window["start_block"], window["end_block"]
            found = window_transfers(portal, token, start, end, 10**9)
            now = tally_of(found)
            diffs = []
            if not now.matches(
                window["event_count"], int(window["amount_sum"]), window["fingerprint"]
            ):
                diffs.append(
                    {
                        "kind": "recheck_changed",
                        "detail": {
                            "stored_events": window["event_count"],
                            "portal_events": now.events,
                            "stored_amount": window["amount_sum"],
                            "portal_amount": str(now.amount),
                        },
                    }
                )
            if rpc is not None:
                diffs += reconcile_window(rpc, token, start, end, found)
            checked.append([start, end])
            if diffs:
                record_diffs(engine, job_id, start, end, diffs)
                invalidate_window(engine, job_id, start, "recheck_mismatch")
                changed.append({"window": [start, end], "diffs": len(diffs)})
                notify(f"窗口 {start}-{end} 复查发现 {len(diffs)} 处差异，已标记为失败")
    return {
        "job_id": job_id,
        "windows_checked": len(checked),
        "changed_windows": changed,
        "ok": not changed,
        "usage": usage(portal, rpc),
    }


def redo_job(engine, job_id: str, *, first=None, last=None, below_version=None):
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
