"""One manually started full run: fixed cutoff, resumable ingestion, final audit.

Safe stop: create the report's .stop sibling. It stops between durable pages,
windows or discovery chunks. This script never schedules another run.
"""

import argparse
import json
import os
import sys
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy.exc import SQLAlchemyError

from chainscope.config import ROOT, load_settings
from chainscope.database import database_error, engine_for
from chainscope.errors import ChainScopeError
from chainscope.holders import top_holders, verify_holders
from chainscope.hybrid import discovery_mark, sync_hybrid
from chainscope.jobs import job_lock, job_status, load_job, verify_job
from chainscope.query import summarize, swap_summary
from chainscope.reconcile import finalized_block
from chainscope.rpc_client import RpcClient
from chainscope.sync import block_time, portal_for


def analysis_complete(analysis, target, scanned):
    """A full event watermark cannot stand in for a current, valid balance cache."""
    holders = analysis["holders"]
    return (
        analysis["holders_on_chain_check"]["ok"]
        and holders["as_of_block"] == target
        and holders["negative_balances"] == 0
        and scanned >= target
        and analysis["swaps"]["coverage"]["state"] == "complete"
    )


def main():
    parser = argparse.ArgumentParser(description="BEM 手动全量采集与结束核验")
    parser.add_argument("--until-block", type=int)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--trace-memory", action="store_true")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = load_settings()
    engine = engine_for(settings)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report = args.report or ROOT / "reports" / f"bem-hybrid-full-{stamp}.json"
    report = report.resolve()
    if report.parent != (ROOT / "reports").resolve():
        raise ChainScopeError("运行报告必须位于项目 reports 目录")
    if report.exists():
        raise ChainScopeError("运行报告已存在；请使用新文件名，不覆盖旧证据")
    report.parent.mkdir(exist_ok=True)
    stop_file = report.with_suffix(".stop")
    log_path = ROOT / "logs" / f"{report.stem}.jsonl"
    state = dict(
        job_id="bem",
        pid=os.getpid(),
        started_at_utc=datetime.now(UTC).isoformat(),
        status="starting",
        primary="alchemy",
        supplemental="sqd",
        transfer_validation="alchemy_local; no full SQD Transfer comparison",
        scheduled=False,
        scope="Alchemy Transfers committed per window; independent PancakeSwap V2/V3 Swap supplementation",
        report=str(report),
        stop_file=str(stop_file),
        log=str(log_path),
    )
    last_snapshot = 0.0
    needs_review = False

    def save():
        state["updated_at_utc"] = datetime.now(UTC).isoformat()
        temporary = report.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(report)

    def notify(message):
        nonlocal last_snapshot, needs_review
        print(message, flush=True)
        state["last_progress"] = message
        try:
            event = json.loads(message)
        except (ValueError, TypeError):
            event = {}
        if "phase" in event:
            state["status"] = event["phase"]
        if "quarantined" in event:
            needs_review = True
            state["attention"] = event
        if time.monotonic() - last_snapshot >= 15 or "phase" in event:
            state["progress"] = job_status(
                engine, "bem", head=state.get("target_block")
            )
            last_snapshot = time.monotonic()
        save()

    code = 1
    save()
    if args.trace_memory:
        tracemalloc.start()
    try:
        with (
            httpx.Client(
                timeout=httpx.Timeout(120, connect=15), follow_redirects=False
            ) as http,
            RpcClient(settings.require_rpc()) as rpc,
        ):
            with engine.connect() as c:
                job = load_job(c, "bem")
            if (
                job["token_address"] != "0x5ce033b2bfca3af30b3e8c8457deaf776a8b695a"
                or job["deploy_block"] != job["start_block"]
            ):
                raise ChainScopeError("BEM 合约或全历史任务起点不符")
            head = finalized_block(rpc)
            target = head if args.until_block is None else args.until_block
            if not job["start_block"] <= target <= head:
                raise ChainScopeError("截止区块不在已最终确认的历史范围内")
            state["target_block"] = target
            state["cutoff_time_beijing"] = block_time(rpc, target)
            state["before"] = job_status(engine, "bem", head=target)
            save()
            portal = portal_for(settings, http, notify)
            state["sync"] = sync_hybrid(
                engine,
                "bem",
                portal,
                rpc,
                until_block=target,
                notify=notify,
                log_path=log_path,
                stop_requested=lambda: stop_file.exists() or needs_review,
            )
            state["progress"] = job_status(engine, "bem", head=target)
            save()
            if state["sync"]["quarantined_remaining"]:
                state["status"], code = "quarantined_requires_review", 3
            elif state["sync"]["stopped"] == "stop_requested":
                state["status"], code = "stopped_by_request", 2
            elif not state["sync"]["coverage_complete"]:
                state["status"], code = state["sync"]["stopped"], 2
            else:
                state["status"] = "final_audit"
                save()
                with job_lock(engine, "bem"):
                    state["database_verification"] = verify_job(engine, "bem")
                    if not state["database_verification"]["ok"]:
                        raise ChainScopeError(
                            "最终数据库核验不通过，不发布完整历史分析"
                        )
                    state["analysis"] = dict(
                        transfers=summarize(engine, job, job["start_block"], target),
                        swaps=swap_summary(engine, job, job["start_block"], target),
                        holders=top_holders(engine, "bem", top=80),
                        holders_on_chain_check=verify_holders(
                            engine, rpc, "bem", top=20
                        ),
                    )
                    _, scanned = discovery_mark(engine, job)
                    state["pool_discovery_through"] = scanned
                checked = analysis_complete(state["analysis"], target, scanned)
                state["status"], code = (
                    ("completed", 0)
                    if checked
                    else ("analysis_requires_review", 3)
                )
    except KeyboardInterrupt:
        state["status"], code = "interrupted", 2
    except (
        ChainScopeError,
        SQLAlchemyError,
        httpx.HTTPError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
    ) as exc:
        state["status"] = "stopped_with_error"
        state["error_type"] = type(exc).__name__
        state["error"] = (
            str(exc)
            if isinstance(exc, ChainScopeError)
            else str(database_error(exc))
            if isinstance(exc, SQLAlchemyError)
            else type(exc).__name__
        )
    finally:
        if args.trace_memory:
            state["memory_peak_mib"] = round(
                tracemalloc.get_traced_memory()[1] / 2**20, 1
            )
            tracemalloc.stop()
        state["finished_at_utc"] = datetime.now(UTC).isoformat()
        save()
        engine.dispose()
    print(
        json.dumps(
            {"status": state["status"], "report": str(report)}, ensure_ascii=False
        ),
        flush=True,
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
