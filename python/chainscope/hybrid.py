"""Commit Alchemy windows during collection; supplement Swaps independently."""

import hashlib
import json
import time
from uuid import uuid4

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import insert as upsert

from .collector import transfer_identities
from .database import fail_batch, start_batch, utcnow
from .dex import FACTORIES, discover_pools, load_pools
from .errors import (
    ChainScopeError,
    DataMismatch,
    HeadersUnavailable,
    ReconcileMismatch,
    SourceUnavailable,
)
from .holders import refresh_balances
from .ingestion import (
    checkpoint,
    collect_window,
    collection_progress,
    pending_rows,
    prepare,
    save_page,
)
from .jobs import (
    QUARANTINE,
    begin_window,
    fail_window,
    job_lock,
    job_status,
    load_job,
    plan_windows,
    record_diffs,
    recover_abandoned,
    reset_stale_pool_windows,
    split_window,
    store_window,
)
from .models import pending_transfers, pool_scans, windows
from .reconcile import (
    compare_sources,
    finalized_block,
    sample_receipts,
)
from .sqd import PARSER_VERSION, TooManyEvents, window_activity
from .sync import Budget, RunLog, event_row, require_schema, say, tally_of, usage

DISCOVERY_CHUNK = 50_000


def quarantine(engine, job, window, exc):
    attempt = str(uuid4())
    start_batch(
        engine,
        attempt,
        job["token_address"],
        window["start_block"],
        window["end_block"],
        None,
        provider="alchemy",
        job_id=job["job_id"],
    )
    begin_window(engine, job["job_id"], window["start_block"], attempt)
    record_diffs(
        engine, job["job_id"], window["start_block"], window["end_block"], exc.diffs
    )
    fail_window(engine, job["job_id"], window["start_block"], exc.kind)
    fail_batch(engine, attempt, exc.kind)


def split(engine, job, window, log):
    start, end = window["start_block"], window["end_block"]
    middle, second = split_window(engine, job["job_id"], start, end)
    # Retain old rows as evidence. The changed end forces a new generation;
    # a crash before this move is safe too: the next attempt refetches each half.
    with engine.begin() as c:
        c.execute(
            pending_transfers.update()
            .where(
                pending_transfers.c.job_id == job["job_id"],
                pending_transfers.c.start_block == start,
                pending_transfers.c.block_number >= second,
            )
            .values(start_block=second)
        )
    log({"split": [start, end], "into": [[start, middle], [second, end]]})


def sqd_fallback(engine, job, window, portal, limit, hard_limit):
    saved = prepare(engine, job["job_id"], window, provider="sqd")
    if saved["complete"]:
        return
    events, _ = window_activity(
        portal,
        job["token_address"],
        window["start_block"],
        window["end_block"],
        limit,
        None,
        hard_limit,
    )
    rows = [
        {
            k: e[k]
            for k in (
                "tx_hash",
                "log_index",
                "block_number",
                "from_address",
                "to_address",
                "block_time_utc",
            )
        }
        | {"amount_raw": str(e["amount"])}
        for e in events
    ]
    save_page(engine, job["job_id"], window, saved, rows, None)


def discovery_mark(engine, job):
    configuration = hashlib.sha256(
        json.dumps(FACTORIES, sort_keys=True).encode()
    ).hexdigest()
    first = (
        job["deploy_block"] if job["deploy_block"] is not None else job["start_block"]
    )
    with engine.connect() as c:
        row = (
            c.execute(sa.select(pool_scans).where(pool_scans.c.job_id == job["job_id"]))
            .mappings()
            .first()
        )
    through = (
        row["scanned_through"]
        if row and row["configuration"] == configuration
        else first - 1
    )
    return configuration, through


def discover_chunk(engine, job, portal, configuration, first, last):
    result = discover_pools(engine, portal, job, first, last)
    # Commit the checkpoint AFTER pools and their rewinds. A crash between the
    # commits repeats an idempotent chunk, never skips an unsaved discovery.
    with engine.begin() as c:
        values = dict(
            job_id=job["job_id"],
            configuration=configuration,
            scanned_through=last,
            updated_at_utc=utcnow(),
        )
        c.execute(
            upsert(pool_scans)
            .values(**values)
            .on_duplicate_key_update(
                **{k: v for k, v in values.items() if k != "job_id"}
            )
        )
    return result


def next_verification(engine, job_id, head):
    with engine.connect() as c:
        row = (
            c.execute(
                sa.select(windows)
                .where(
                    windows.c.job_id == job_id,
                    windows.c.end_block <= head,
                    windows.c.status.in_(("pending", "failed")),
                    windows.c.error_type.is_(None)
                    | windows.c.error_type.not_in(QUARANTINE),
                )
                .order_by(windows.c.start_block)
                .limit(1)
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def promote(
    engine,
    job,
    window,
    portal,
    rpc,
    pools,
    head,
    limit,
    hard_limit,
    stop=lambda: False,
    notify=lambda _: None,
    allowance=lambda: None,
):
    """Admit one complete primary window using local checks, not a second download."""
    from .headers import complete_headers, confirm_transfer_times

    staged = pending_rows(engine, job["job_id"], window)
    with engine.connect() as c:
        saved = checkpoint(c, job["job_id"], window["start_block"])
        from .transaction_checks import reject_conflicting_transactions

        reject_conflicting_transactions(c, job["chain_id"], staged, rpc, stop, job["job_id"])
    token, start, end = job["token_address"], window["start_block"], window["end_block"]
    policy = "local_checked"
    if saved["provider"] == "sqd":
        # Preserve the user's stricter admission requirement for fallback data.
        secondary, _ = transfer_identities(rpc, token, start, end)
        diffs = compare_sources(staged, secondary)
        if diffs:
            raise ReconcileMismatch("备用来源转账与恢复后的主来源不一致", diffs[:1000])
        policy = "fallback_match"
    headers = complete_headers(
        engine,
        job,
        window,
        staged,
        portal,
        rpc,
        stop,
        notify=notify,
        allowance=allowance,
    )
    if headers is None:
        return None
    time_confirmations = confirm_transfer_times(staged, headers, rpc, stop)
    if time_confirmations is None:
        return None
    events = []
    for item in staged:
        header = headers[item["block_number"]]
        events.append(
            {
                k: item[k]
                for k in (
                    "tx_hash",
                    "log_index",
                    "block_number",
                    "from_address",
                    "to_address",
                    "amount",
                )
            }
            | {
                "chain_id": 56,
                "token_address": token,
                "block_hash": header["block_hash"],
                "block_time_utc": header["block_time_utc"],
            }
        )
    rows = [event_row(e, job["token_decimals"]) for e in events]
    if time_confirmations:
        affected = {(r["tx_hash"], r["log_index"]) for r in time_confirmations}
        corrected = [r for r in rows if (r["tx_hash"], r["log_index"]) in affected]
        # Check every affected transaction, in addition to the ordinary sample.
        # Block time alone does not prove an event belongs to that block.
        sample_receipts(rpc, corrected, start, count=len(corrected))
    receipts = sample_receipts(rpc, rows, start)
    if stop():
        return None
    attempt = str(uuid4())
    start_batch(
        engine,
        attempt,
        token,
        start,
        end,
        None,
        provider="alchemy" if policy == "local_checked" else "alchemy+sqd",
        job_id=job["job_id"],
    )
    begin_window(engine, job["job_id"], start, attempt)
    try:
        return store_window(
            engine,
            job,
            window,
            attempt,
            rows,
            tally_of(events),
            head,
            sleep=time.sleep,
            swap_rows=None,
            extra={
                "reconcile": policy,
                "receipt_checked": receipts,
                "parser_version": PARSER_VERSION,
                "header_time_confirmations": time_confirmations,
            },
        )
    except (ChainScopeError, sa.exc.SQLAlchemyError):
        fail_window(engine, job["job_id"], start, "admission_interrupted")
        fail_batch(engine, attempt, "admission_interrupted")
        raise


def sync_hybrid(
    engine,
    job_id,
    portal,
    rpc,
    *,
    phase="all",
    until_block=None,
    max_windows=None,
    max_seconds=None,
    max_requests=None,
    max_compute_units=None,
    event_limit=50_000,
    block_hard_limit=50_000,
    clock=time.monotonic,
    sleep=time.sleep,
    notify=say,
    log_path=None,
    stop_requested=lambda: False,
):
    require_schema(engine)
    if phase not in ("all", "collect", "supplement"):
        raise ChainScopeError("未知采集阶段")
    if max_windows is not None and max_windows < 1:
        raise ChainScopeError("窗口上限必须为正数")
    if event_limit < 1 or block_hard_limit < event_limit:
        raise ChainScopeError("事件上限不正确")
    log = RunLog(notify, log_path)
    budget = Budget(
        clock,
        seconds=max_seconds,
        requests=max_requests,
        compute_units=max_compute_units,
    )
    summary = dict(
        job_id=job_id,
        primary="alchemy",
        supplemental="sqd",
        phase=phase,
        windows_collected=0,
        windows_done=0,
        swap_windows_done=0,
        inserted=0,
        quarantined=[],
        splits=0,
        source_switches=[],
        stopped=None,
    )
    with job_lock(engine, job_id):
        with engine.connect() as c:
            job = load_job(c, job_id)
        if job["status"] != "active":
            raise ChainScopeError("任务已暂停")
        summary["recovered_abandoned"] = recover_abandoned(engine, job_id)
        primary_available = True
        try:
            head = finalized_block(rpc)
        except SourceUnavailable:
            primary_available = False
            head = portal.finalized_head()
            summary["source_switches"].append("alchemy_unavailable_to_sqd_pending")
        if until_block is not None:
            if until_block < job["start_block"] or until_block > head:
                raise ChainScopeError("本次截止区块早于任务起点或尚未最终确认")
            head = until_block
        if job["end_block"] is not None:
            head = min(head, job["end_block"])
        summary["target_block"] = head
        plan_windows(engine, job_id, head)
        with engine.connect() as c:
            crossing = c.execute(
                sa.select(windows.c.start_block).where(
                    windows.c.job_id == job_id,
                    windows.c.start_block <= head,
                    windows.c.end_block > head,
                    windows.c.status != "done",
                )
            ).first()
        if crossing:
            raise ChainScopeError(
                "截止位置落在已有未完成窗口内部；请使用该窗口结束位置，避免越过本次截止区块"
            )

        def exhausted():
            if stop_requested():
                return "stop_requested"
            return budget.exhausted(portal, rpc)

        def requests_left():
            if budget.requests is None:
                return None
            return budget.requests - portal.stats["requests"] - rpc.requests

        def fetch(window):
            nonlocal primary_available
            if primary_available:
                try:
                    return collect_window(
                        engine,
                        job,
                        window,
                        rpc,
                        limit=event_limit,
                        hard_limit=block_hard_limit,
                        stop=exhausted,
                        notify=log,
                    )
                except SourceUnavailable:
                    primary_available = False
                    summary["source_switches"].append(
                        "alchemy_unavailable_to_sqd_pending"
                    )
                    log({"source_switch": "sqd", "admission": "pending_only"})
            sqd_fallback(engine, job, window, portal, event_limit, block_hard_limit)
            return True

        from . import supplement

        with engine.begin() as c:
            supplement.enable(c, job_id)
        # Visit existing completed staging windows first, then continue primary
        # collection. A split retries only the affected half; no history reset.
        log(
            {
                "phase": "collect" if phase != "supplement" else "admit_saved",
                "target_block": head,
                "transfer_validation": "alchemy_local",
            }
        )
        after = -1
        while not exhausted():
            if max_windows is not None and summary["windows_collected"] >= max_windows:
                summary["stopped"] = "max_windows"
                break
            with engine.connect() as c:
                row = (
                    c.execute(
                        sa.select(windows)
                        .where(
                            windows.c.job_id == job_id,
                            windows.c.start_block > after,
                            windows.c.end_block <= head,
                            windows.c.status.in_(("pending", "failed")),
                            windows.c.error_type.is_(None)
                            | windows.c.error_type.not_in(QUARANTINE),
                        )
                        .order_by(windows.c.start_block)
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                saved = checkpoint(c, job_id, row["start_block"]) if row else None
            if row is None:
                break
            window = dict(row)
            if phase == "supplement" and (
                not saved
                or not saved["complete"]
                or saved["end_block"] != window["end_block"]
            ):
                after = window["start_block"]
                continue
            try:
                if phase != "supplement" and not fetch(window):
                    break
                summary["windows_collected"] += 1
                if phase != "collect" and primary_available and not exhausted():
                    result = promote(
                        engine,
                        job,
                        window,
                        portal,
                        rpc,
                        {},
                        head,
                        event_limit,
                        block_hard_limit,
                        stop=exhausted,
                        notify=log,
                        allowance=requests_left,
                    )
                    if result is None:
                        break
                    summary["windows_done"] += 1
                    summary["inserted"] += result.get("inserted", 0)
                    log(
                        {
                            "committed_window": [
                                window["start_block"],
                                window["end_block"],
                            ],
                            "transfers_committed_through": result["through"],
                            "transfer_validation": "alchemy_local",
                        }
                    )
                    summary["balances"] = refresh_balances(
                        engine, job_id, stop=exhausted, sleep=sleep
                    )
                after = window["start_block"]
            except TooManyEvents:
                split(engine, job, window, log)
                summary["splits"] += 1
            except DataMismatch as exc:
                quarantine(engine, job, window, exc)
                summary["quarantined"].append(
                    [window["start_block"], window["end_block"]]
                )
                log(
                    {
                        "quarantined": [window["start_block"], window["end_block"]],
                        "reason": exc.kind,
                    }
                )
                after = window["start_block"]
            except HeadersUnavailable as exc:
                summary["stopped"] = "block_headers_unavailable"
                summary["headers_error"] = str(exc)
                log({"block_headers": "paused", "reason": str(exc)})
                break
            except SourceUnavailable:
                summary["stopped"] = "awaiting_primary_admission"
                break

        if phase == "all" or phase == "supplement":
            with engine.connect() as c:
                job = load_job(c, job_id)
            if (
                job["verified_through"] is not None
                and not exhausted()
                and not summary["stopped"]
            ):
                log({"phase": "supplement", "target_block": head})
                try:
                    sqd_head = min(
                        head, portal.finalized_head(), job["verified_through"]
                    )
                    configuration, scanned = discovery_mark(engine, job)
                    while not exhausted():
                        if (
                            max_windows is not None
                            and summary["swap_windows_done"] >= max_windows
                        ):
                            summary["stopped"] = "max_windows"
                            break
                        frontier = min(scanned, sqd_head)
                        ready = supplement.next_window(engine, job, frontier)
                        if ready is not None:
                            with engine.connect() as c:
                                pools = load_pools(c, job_id)
                            try:
                                result = supplement.collect(
                                    engine,
                                    job,
                                    ready,
                                    portal,
                                    rpc,
                                    pools,
                                    event_limit,
                                    block_hard_limit,
                                )
                                summary["swap_windows_done"] += 1
                                log(
                                    {
                                        "swap_window": [
                                            ready["start_block"],
                                            ready["end_block"],
                                        ],
                                        "swaps_verified_through": result["through"],
                                    }
                                )
                            except TooManyEvents:
                                supplement.split(engine, job_id, ready)
                                summary["splits"] += 1
                            except DataMismatch as exc:
                                with engine.begin() as c:
                                    c.execute(
                                        supplement.swap_windows.update()
                                        .where(
                                            supplement.swap_windows.c.job_id == job_id,
                                            supplement.swap_windows.c.start_block
                                            == ready["start_block"],
                                        )
                                        .values(status="failed", error_type=exc.kind)
                                    )
                                record_diffs(
                                    engine,
                                    job_id,
                                    ready["start_block"],
                                    ready["end_block"],
                                    exc.diffs,
                                )
                                summary["quarantined"].append(
                                    [ready["start_block"], ready["end_block"]]
                                )
                                log(
                                    {
                                        "quarantined": [
                                            ready["start_block"],
                                            ready["end_block"],
                                        ],
                                        "scope": "swaps_only",
                                        "reason": exc.kind,
                                    }
                                )
                            continue
                        if scanned >= sqd_head:
                            break
                        low, high = (
                            scanned + 1,
                            min(scanned + DISCOVERY_CHUNK, sqd_head),
                        )
                        result = discover_chunk(
                            engine, job, portal, configuration, low, high
                        )
                        scanned = high
                        summary["pool_discovery_through"] = scanned
                        log(
                            {
                                "pool_scan": [low, high],
                                "added": len(result["added"]),
                                "swap_windows_reset": result["windows_reset"],
                            }
                        )
                    if sqd_head < head and not summary["stopped"]:
                        summary["stopped"] = "supplement_not_at_cutoff"
                except (ChainScopeError, httpx.HTTPError) as exc:
                    if isinstance(exc, DataMismatch):
                        raise
                    summary["stopped"] = "awaiting_supplement"
                    summary["supplement_error"] = (
                        str(exc)
                        if isinstance(exc, ChainScopeError)
                        else type(exc).__name__
                    )
                    log(
                        {
                            "phase": "awaiting_supplement",
                            "reason": summary["supplement_error"],
                        }
                    )

        with engine.connect() as c:
            pools = load_pools(c, job_id)
        reset_stale_pool_windows(engine, job_id, pools)
        progress = job_status(engine, job_id, head=head)
        transfers_complete = progress["lag_blocks"] == 0
        swaps_complete = progress["swaps_complete"]
        summary.update(
            verified_through=progress["verified_through"],
            transfers_complete=transfers_complete,
            swaps_complete=swaps_complete,
            swaps_verified_through=progress["swaps_verified_through"],
            quarantined_remaining=progress["quarantined_windows"]
            + progress["swap_windows"].get("failed", 0),
            coverage_complete=transfers_complete and swaps_complete,
        )
        summary["stopped"] = (
            summary["stopped"]
            or exhausted()
            or ("caught_up" if summary["coverage_complete"] else "awaiting_supplement")
        )
        try:
            summary["balances"] = refresh_balances(
                engine, job_id, stop=exhausted, sleep=sleep
            )
        except ChainScopeError as exc:
            summary["balances"] = {"complete": False, "error": str(exc)}
        with engine.connect() as c:
            summary.update(collection_progress(c, job_id))
    summary["elapsed_seconds"] = round(budget.elapsed(), 1)
    summary["usage"] = usage(portal, rpc)
    return summary
