"""Detached collector. The model and chat process are not its lifetime owner."""

import argparse
import json
import os
import subprocess
import sys
import threading
import time

import psutil
from sqlalchemy.exc import SQLAlchemyError

from .agent_backend import Backend, completion
from .agent_contracts import Counters, Fault
from .agent_store import Store, shown
from .config import ROOT, load_settings
from .database import engine_for
from .errors import ChainScopeError, DataMismatch, SourceUnavailable
from .token_registry import AgentError, Registry


def process_alive(pid, created):
    try:
        p = psutil.Process(pid)
        return (
            abs(p.create_time() - created) < 0.01
            and p.is_running()
            and p.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, TypeError, ValueError):
        return None


def launch(run_id, ticket):
    # Only server-generated UUIDs reach argv. Never shell=True; no provider keys in argv.
    from .agent_contracts import RunInput, StartInput

    RunInput(run_id=run_id)
    StartInput(plan_id=ticket)
    (ROOT / "logs").mkdir(exist_ok=True)
    with (ROOT / "logs" / f"agent-{run_id}.log").open("ab") as log:
        flags = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt"
            else 0
        )
        return subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "chainscope.agent_worker",
                "--run",
                run_id,
                "--ticket",
                ticket,
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )


def execute_worker(store, backend, run_id, ticket, *, heartbeat_interval=5):
    claim = store.claim(run_id, ticket, os.getpid(), psutil.Process().create_time())
    if claim is None:
        return False
    row, data, attempt_id = claim
    _, plan = store.plan(data.plan_id, row["owner_id"])
    base_seconds, base_requests = (
        data.budget.used_runtime_seconds,
        data.budget.used_http_attempts,
    )
    started = time.monotonic()
    meter = {"read": lambda: 0, "elapsed": lambda: time.monotonic() - started}
    finished, heartbeat_failed = threading.Event(), threading.Event()
    attention = False
    policy_problem = None
    last_snapshot, last_stop_check = 0.0, 0.0
    stopped_cached = False
    data_lock = threading.Lock()

    def update_usage():
        data.budget.used_runtime_seconds = base_seconds + meter["elapsed"]()
        data.budget.used_http_attempts = (
            base_requests + meter["read"]() + getattr(backend, "validation_requests", 0)
        )
        data.last_heartbeat_at = shown()

    def heartbeat():
        while not finished.wait(heartbeat_interval):
            try:
                with data_lock:
                    update_usage()
                    store.save_run(run_id, ticket, data, attempt_id=attempt_id)
            except (SQLAlchemyError, AgentError):
                heartbeat_failed.set()
                return

    thread = threading.Thread(target=heartbeat, name="agent-heartbeat", daemon=True)
    thread.start()

    def stop():
        nonlocal last_stop_check, stopped_cached, policy_problem
        if heartbeat_failed.is_set() or attention or policy_problem is not None:
            return True
        if time.monotonic() - last_stop_check >= 1:
            last_stop_check = time.monotonic()
            stopped_cached = store.should_stop(run_id, ticket)
            try:
                registry = Registry.load()
                token = registry.resolve(plan.token.token_id)
                if registry.fingerprint(token) != plan.registry_digest:
                    raise AgentError("PLAN_STALE", "服务列表或策略在运行中发生变化")
            except AgentError as exc:
                policy_problem = exc
                return True
        return stopped_cached

    def notify(message):
        nonlocal last_snapshot, attention
        event = json.loads(message)
        if "quarantined" in event:
            attention = True
        with data_lock:
            data.phase = event.get("phase", data.phase)
            update_usage()
            if (
                time.monotonic() - last_snapshot >= plan.policy.poll_interval_seconds
                or attention
            ):
                _, data.progress = backend.inspect(plan.token)
                last_snapshot = time.monotonic()
                store.save_run(run_id, ticket, data, attempt_id=attempt_id)

    try:
        if stop():
            summary = {"stopped": "stop_requested"}
        else:
            backend.validate(plan)
            with data_lock:
                data.state, data.phase = "running", "collect"
                store.save_run(run_id, ticket, data, attempt_id=attempt_id)
            if (
                data.budget.used_runtime_seconds >= data.budget.max_runtime_seconds
                or data.budget.used_http_attempts >= data.budget.max_http_attempts
            ):
                summary = {"stopped": "budget_exhausted"}
            else:
                summary = backend.execute(plan, notify, stop, meter)
        with data_lock:
            update_usage()
            _, data.progress = backend.inspect(plan.token)
            data.completion = completion(data.progress, plan.target.block)
            if attention or summary.get("quarantined_remaining") or policy_problem:
                data.state, data.recovery = "needs_attention", "human_action_required"
                data.last_error = Fault(
                    code=policy_problem.code
                    if policy_problem
                    else "DATA_REVIEW_REQUIRED",
                    message=str(policy_problem)
                    if policy_problem
                    else "采集发现数据冲突，请处理隔离窗口",
                    recovery="human_action_required",
                    related_run_id=run_id,
                )
            elif data.completion.complete:
                data.state, data.recovery = "completed", "none"
            elif store.should_stop(run_id, ticket):
                data.state, data.recovery, data.stop_effective_at = (
                    "stopped",
                    "resume_same_target",
                    shown(),
                )
            else:
                data.state, data.recovery = "paused", "resume_same_target"
                reason = summary.get("stopped") or "incomplete"
                data.last_error = Fault(
                    code=str(reason).upper(),
                    message=summary.get("headers_error")
                    or summary.get("supplement_error")
                    or "本次执行已暂停，固定目标仍未全部完成",
                    recovery="resume_same_target",
                    related_run_id=run_id,
                )
            before = data.run_counters

            def total(previous, value):
                # None after the first attempt means an earlier attempt failed
                # uncounted; adding to zero would understate the run's total.
                if previous is None and data.attempt_count > 1:
                    return None
                return (previous or 0) + value

            data.run_counters = Counters(
                inserted_transfers=total(
                    before.inserted_transfers, summary.get("inserted", 0)
                ),
                transfer_windows=total(
                    before.transfer_windows, summary.get("windows_done", 0)
                ),
                swap_windows=total(
                    before.swap_windows, summary.get("swap_windows_done", 0)
                ),
                inserted_swaps=None,  # Existing collector doesn't attribute exact inserted Swap rows per run.
            )
    except Exception as exc:  # noqa: BLE001 -- detached process boundary; never print provider/SQL exception text.
        # The class name alone is safe and tells the run log what stopped it.
        print(f"worker stopped by {type(exc).__name__}", flush=True)
        with data_lock:
            update_usage()
            if isinstance(exc, (DataMismatch, AgentError)):
                state, recovery = "needs_attention", "human_action_required"
                code = (
                    exc.code if isinstance(exc, AgentError) else "DATA_REVIEW_REQUIRED"
                )
            elif isinstance(exc, SourceUnavailable):
                state, recovery, code = (
                    "paused",
                    "resume_same_target",
                    "SOURCE_UNAVAILABLE",
                )
            else:
                state, recovery, code = (
                    "failed",
                    "human_action_required",
                    "WORKER_FAILED",
                )
            data.state, data.recovery = state, recovery
            data.last_error = Fault(
                code=code,
                message="执行未完成；已保留提交进度，需按错误类型处理",
                recovery=recovery,
                related_run_id=run_id,
            )
            data.run_counters = Counters()
    finally:
        finished.set()
        thread.join(timeout=10)
    data.ended_at, data.next_poll_after_seconds = shown(), None
    if data.state == "stopped":
        current, _ = store.run(run_id, row["owner_id"])
        data.stop_requested_at = (
            shown(current["stop_requested_at"])
            if current["stop_requested_at"]
            else None
        )
    store.save_run(run_id, ticket, data, attempt_id=attempt_id, final=True)
    return True


def main():
    parser = argparse.ArgumentParser(description="内部采集进程；请通过聊天工具启动")
    parser.add_argument("--run", required=True)
    parser.add_argument("--ticket", required=True)
    args = parser.parse_args()
    from .agent_contracts import RunInput

    RunInput(run_id=args.run)  # A UUID, so it is safe inside a file name.
    settings = load_settings()
    engine = engine_for(settings)
    try:
        store = Store(engine)
        store.require_ready()
        backend = Backend(engine, settings)
        backend.log_path = ROOT / "logs" / f"agent-{args.run}.jsonl"
        execute_worker(store, backend, args.run, args.ticket)
    except (SQLAlchemyError, AgentError, ChainScopeError, OSError):
        print("后台执行记录未能确认；请查询原运行状态，不重复启动。", flush=True)
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
