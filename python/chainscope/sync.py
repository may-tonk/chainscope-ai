"""Create scan jobs and run them window by window, resuming after any stop."""

import json
import re
import sys
import time
from uuid import uuid4

import httpx
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings
from .database import check_database, engine_for, fail_batch, start_batch
from .errors import ChainScopeError
from .jobs import (
    begin_window,
    create_job,
    fail_window,
    job_lock,
    load_job,
    next_window,
    plan_windows,
    recover_abandoned,
    split_window,
    store_window,
    window_tally,
)
from .parser import ADDRESS, fixed_hex, quantity
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


def sync_job(
    engine,
    job_id: str,
    portal: Portal,
    *,
    max_windows: int | None = None,
    max_seconds: float | None = None,
    event_limit: int = DEFAULT_EVENT_LIMIT,
    clock=time.monotonic,
    sleep=time.sleep,
    notify=say,
) -> dict:
    """Collect pending windows in block order until caught up or out of budget."""
    require_schema(engine)
    started = clock()
    summary = {
        "job_id": job_id,
        "windows_done": 0,
        "events": 0,
        "inserted": 0,
        "splits": 0,
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
        head = portal.finalized_head()
        summary["source_head"] = head
        summary["planned_windows"] = plan_windows(engine, job_id, head)
        while True:
            if max_windows is not None and summary["windows_done"] >= max_windows:
                summary["stopped"] = "max_windows"
                break
            if max_seconds is not None and clock() - started >= max_seconds:
                summary["stopped"] = "max_runtime"
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
                rows = [event_row(e, job["token_decimals"]) for e in found]
                tally = Tally()
                for e in found:
                    tally.add(
                        e["tx_hash"],
                        e["log_index"],
                        e["from_address"],
                        e["to_address"],
                        e["amount"],
                    )
                result = store_window(
                    engine, job, window, attempt, rows, tally, head, sleep=sleep
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
                notify(
                    f"窗口 {start}-{end} 事件超过 {event_limit} 条，拆成 {start}-{middle} 和 {second}-{end}"
                )
                continue
            except FAILURES as exc:
                try:
                    fail_window(engine, job_id, start, type(exc).__name__)
                    fail_batch(engine, attempt, type(exc).__name__)
                except SQLAlchemyError:
                    pass  # left 'running'; the next run marks it abandoned
                raise
            summary["windows_done"] += 1
            summary["events"] += tally.events
            summary["inserted"] += result.get("inserted", 0)
            summary["verified_through"] = result["through"]
            notify(
                json.dumps(
                    {
                        "window": [start, end],
                        "events": tally.events,
                        "inserted": result.get("inserted"),
                        "seconds": round(clock() - tick, 2),
                        "verified_through": result["through"],
                    },
                    ensure_ascii=False,
                )
            )
    summary["elapsed_seconds"] = round(clock() - started, 1)
    summary["portal_usage"] = dict(portal.stats)
    return summary


def run_sync(
    settings: Settings,
    job_id: str,
    *,
    max_windows: int | None = None,
    max_seconds: float | None = None,
    event_limit: int = DEFAULT_EVENT_LIMIT,
) -> dict:
    engine = engine_for(settings)
    try:
        with httpx.Client(
            timeout=httpx.Timeout(120, connect=15), follow_redirects=False
        ) as http:
            portal = Portal(DEFAULT_PORTAL, DEFAULT_DATASET, http, notify=say)
            return sync_job(
                engine,
                job_id,
                portal,
                max_windows=max_windows,
                max_seconds=max_seconds,
                event_limit=event_limit,
            )
    finally:
        engine.dispose()
