"""Durable result snapshots and local wakeups. Wakeups never contain result data."""

import copy
import json
import os
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
from pathlib import Path
from uuid import UUID, uuid4

import psutil
import sqlalchemy as sa

from .agent_contracts import Delivery, EventData, HistoryData, RunData, RunSummary
from .agent_models import attempts, events, plans, runs
from .agent_store import now, shown
from .token_registry import AgentError

REPORTABLE = ("run_finished", "run_unconfirmed")


def address(owner):
    identity = str(UUID(owner))
    if os.name == "nt":
        return rf"\\.\pipe\chainscope-agent-{identity}", "AF_PIPE"
    return str(Path(tempfile.gettempdir()) / f"chainscope-{identity}.sock"), "AF_UNIX"


def wake(owner, run_id=None, *, closing=False):
    """Best effort only: the committed event remains authoritative if chat is closed."""
    endpoint, family = address(owner)
    try:
        with Client(endpoint, family=family, authkey=UUID(owner).bytes) as channel:
            channel.send_bytes(
                json.dumps({"run_id": run_id, "closing": closing}).encode()
            )
    except (OSError, EOFError, AuthenticationError):
        pass


class LocalEvents:
    def __init__(self, owner, callback):
        endpoint, family = address(owner)
        self.owner, self.callback = owner, callback
        self.listener = Listener(endpoint, family=family, authkey=UUID(owner).bytes)
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._receive, daemon=True)
        self.thread.start()

    def _receive(self):
        while not self.closed.is_set():
            try:
                with self.listener.accept() as channel:
                    if not channel.poll(2):
                        continue
                    message = json.loads(channel.recv_bytes(512))
                    if not isinstance(message, dict):
                        continue
                    if message.get("closing"):
                        self.closed.set()
                        continue
                    identity = message.get("run_id")
                    if identity is not None:
                        identity = str(UUID(identity))
                    if not self.closed.is_set():
                        self.callback(identity)
            except (OSError, EOFError, ValueError, TypeError, AuthenticationError):
                continue

    def close(self):
        wake(self.owner, closing=True)
        self.thread.join(timeout=3)
        self.closed.set()
        self.listener.close()


class EventJournal:
    def __init__(self, engine):
        self.engine = engine

    @staticmethod
    def finish(c, row, data, *, attempt_id=None, kind="run_finished"):
        # Caller holds the run row lock, so repeated final writes have one event.
        episode = f"{data.plan_id}:{data.attempt_count}"
        existing = c.scalar(
            sa.select(events.c.event_id).where(
                events.c.run_id == data.run_id,
                events.c.kind == kind,
                events.c.payload["episode"].as_string() == episode,
            )
        )
        if existing is not None:
            return existing
        plan = c.execute(
            sa.select(plans.c.payload).where(plans.c.plan_id == data.plan_id)
        ).scalar_one()
        attempt = (
            c.execute(
                sa.select(attempts).where(
                    attempts.c.run_id == data.run_id, attempts.c.ticket == row["ticket"]
                )
            )
            .mappings()
            .first()
        )
        payload = {
            "episode": episode,
            "attempt_id": attempt["attempt_id"] if attempt else attempt_id,
            "attempt_started_at": shown(attempt["started_at"]) if attempt else None,
            "token": plan["token"],
            "snapshot": data.model_dump(mode="json"),
            "report": {"state": "pending", "generation_attempts": 0},
        }
        result = c.execute(
            events.insert().values(
                owner_id=row["owner_id"],
                plan_id=data.plan_id,
                run_id=data.run_id,
                kind=kind,
                occurred_at=now(),
                payload=payload,
            )
        )
        return result.inserted_primary_key[0]

    def owned(self, event_id, owner, *, c=None, lock=False):
        query = sa.select(events).where(
            events.c.event_id == event_id,
            events.c.owner_id == owner,
            events.c.kind.in_(REPORTABLE),
        )
        if lock:
            query = query.with_for_update()
        if c is None:
            with self.engine.connect() as connection:
                row = connection.execute(query).mappings().first()
        else:
            row = c.execute(query).mappings().first()
        if row is None:
            raise AgentError(
                "RECORD_NOT_FOUND", "结束事件不存在或当前身份无权访问", "none"
            )
        return dict(row)

    @staticmethod
    def data(row):
        payload = row["payload"]
        public = {
            k: v for k, v in payload["report"].items() if k in Delivery.model_fields
        }
        return EventData(
            event_id=row["event_id"],
            event_type=row["kind"],
            occurred_at=shown(row["occurred_at"]),
            attempt_id=payload["attempt_id"],
            attempt_started_at=payload["attempt_started_at"],
            token=payload["token"],
            run=payload["snapshot"],
            report=public,
        )

    def get(self, event_id, owner):
        return self.data(self.owned(event_id, owner))

    def pending(self, owner):
        with self.engine.connect() as c:
            return list(
                c.scalars(
                    sa.select(events.c.event_id)
                    .where(
                        events.c.owner_id == owner,
                        events.c.kind.in_(REPORTABLE),
                        events.c.payload["report"]["state"].as_string() != "displayed",
                    )
                    .order_by(events.c.event_id)
                )
            )

    def claim(self, event_id, owner):
        with self.engine.begin() as c:
            row = self.owned(event_id, owner, c=c, lock=True)
            payload = copy.deepcopy(row["payload"])
            report = payload["report"]
            if report["state"] == "displayed":
                return None
            if report.get("lease_id"):
                from .agent_worker import process_alive

                expires = datetime.fromisoformat(report["lease_expires_at"])
                alive = process_alive(report["lease_pid"], report["lease_created"])
                if expires > datetime.now(UTC) and alive is not False:
                    return None
            lease = str(uuid4())
            report.update(
                lease_id=lease,
                lease_pid=os.getpid(),
                lease_created=psutil.Process().create_time(),
                lease_expires_at=(
                    datetime.now(UTC) + timedelta(minutes=15)
                ).isoformat(),
                state="generated" if report.get("text") else "generating",
            )
            if not report.get("text"):
                report["generation_attempts"] += 1
            c.execute(
                events.update()
                .where(events.c.event_id == event_id)
                .values(payload=payload)
            )
            row["payload"] = payload
            return self.data(row), lease

    def update_report(self, event_id, owner, lease, **changes):
        with self.engine.begin() as c:
            row = self.owned(event_id, owner, c=c, lock=True)
            payload = copy.deepcopy(row["payload"])
            report = payload["report"]
            if report.get("lease_id") != lease:
                raise AgentError("REPORT_FENCED", "报告处理权已失效", "none")
            report.update(changes)
            if changes.get("state") in ("displayed", "failed"):
                for key in (
                    "lease_id",
                    "lease_pid",
                    "lease_created",
                    "lease_expires_at",
                ):
                    report.pop(key, None)
            c.execute(
                events.update()
                .where(events.c.event_id == event_id)
                .values(payload=payload)
            )

    def history(self, owner, request):
        latest = (
            sa.select(events.c.run_id, sa.func.max(events.c.event_id).label("event_id"))
            .where(events.c.owner_id == owner, events.c.kind.in_(REPORTABLE))
            .group_by(events.c.run_id)
            .subquery()
        )
        query = (
            sa.select(runs, latest.c.event_id.label("latest_event_id"))
            .outerjoin(latest, latest.c.run_id == runs.c.run_id)
            .where(runs.c.owner_id == owner)
        )
        if request.token_id:
            query = query.where(runs.c.token_id == request.token_id)
        for value, lower in ((request.created_from, True), (request.created_to, False)):
            if value:
                stamp = (
                    datetime.fromisoformat(value).astimezone(UTC).replace(tzinfo=None)
                )
                query = query.where(
                    runs.c.accepted_at >= stamp if lower else runs.c.accepted_at < stamp
                )
        with self.engine.connect() as c:
            if request.before_run_id:
                anchor = c.execute(
                    sa.select(runs.c.accepted_at).where(
                        runs.c.run_id == request.before_run_id,
                        runs.c.owner_id == owner,
                    )
                ).first()
                if anchor is None:
                    raise AgentError("INVALID_CURSOR", "历史分页位置不存在", "none")
                query = query.where(
                    sa.or_(
                        runs.c.accepted_at < anchor[0],
                        sa.and_(
                            runs.c.accepted_at == anchor[0],
                            runs.c.run_id < request.before_run_id,
                        ),
                    )
                )
            rows = (
                c.execute(
                    query.order_by(
                        runs.c.accepted_at.desc(), runs.c.run_id.desc()
                    ).limit(request.limit + 1)
                )
                .mappings()
                .all()
            )
        items = []
        for row in rows[: request.limit]:
            data = RunData.model_validate(row["payload"])
            items.append(
                RunSummary(
                    run_id=data.run_id,
                    token_id=data.token_id,
                    state=data.state,
                    accepted_at=shown(row["accepted_at"]),
                    ended_at=data.ended_at,
                    target=data.target,
                    latest_event_id=row["latest_event_id"],
                )
            )
        return HistoryData(
            items=items,
            next_cursor=items[-1].run_id if len(rows) > request.limit else None,
        )

    def active(self, owner):
        with self.engine.connect() as c:
            return [
                dict(row)
                for row in c.execute(
                    sa.select(runs).where(
                        runs.c.owner_id == owner,
                        runs.c.active_job.is_not(None),
                    )
                ).mappings()
            ]

    def recent_runs(self, owner):
        with self.engine.connect() as c:
            rows = c.scalars(
                sa.select(runs.c.payload)
                .where(runs.c.owner_id == owner)
                .order_by(runs.c.accepted_at.desc())
                .limit(100)
            )
            result = {}
            for payload in rows:
                data = RunData.model_validate(payload)
                result.setdefault(data.token_id, data)
            return result

    def observe_exit(self, run_id, owner, *, expected_ticket=None, exited=False):
        """No takeover: a missing final record creates an uncertainty notification only."""
        from .agent_contracts import Fault
        from .agent_store import TERMINAL
        from .agent_worker import process_alive

        with self.engine.begin() as c:
            row = (
                c.execute(
                    sa.select(runs)
                    .where(
                        runs.c.run_id == run_id,
                        runs.c.owner_id == owner,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row is None:
                return
            if expected_ticket is not None and row["ticket"] != expected_ticket:
                return
            data = RunData.model_validate(row["payload"])
            if row["state"] in TERMINAL:
                self.finish(c, row, data)
                return
            alive = (
                process_alive(row["pid"], row["process_started"])
                if row["pid"]
                else None
            )
            reference = row["heartbeat_at"] or row["accepted_at"]
            if not row["pid"] and data.queued_at:
                reference = (
                    datetime.fromisoformat(data.queued_at)
                    .astimezone(UTC)
                    .replace(tzinfo=None)
                )
            stale = now() - reference > timedelta(seconds=60)
            if exited or alive is False or (row["pid"] is None and stale):
                data.state, data.recovery = "unknown", "human_action_required"
                data.last_error = Fault(
                    code="WORKER_UNCONFIRMED",
                    message="进程已退出或启动未确认，最终状态需要核实；没有自动重启。",
                    recovery="human_action_required",
                    related_run_id=run_id,
                )
                self.finish(c, row, data, kind="run_unconfirmed")


class ProcessWatcher:
    """Watch OS process exits, without polling MySQL or a model."""

    def __init__(self, callback):
        self.callback = callback
        self.closed = threading.Event()
        self.watched = set()
        self.children = {}
        self.guard = threading.Lock()

    def track_child(self, process, run_id, ticket):
        # The launching chat retains the actual process handle. Even a crash
        # between final DB commit and IPC send therefore produces a wakeup.
        with self.guard:
            self.children[process.pid] = process

        def wait():
            process.wait()
            if not self.closed.is_set():
                self.callback((run_id, ticket, True))

        threading.Thread(target=wait, daemon=True).start()

    def track(self, row):
        if not row["pid"]:
            return
        key = row["pid"], row["process_started"]
        with self.guard:
            child = self.children.get(row["pid"])
            if child is not None and child.poll() is None:
                return
            if key in self.watched:
                return
            self.watched.add(key)
        threading.Thread(target=self._wait, args=(row,), daemon=True).start()

    def _wait(self, row):
        from .agent_worker import process_alive

        try:
            process = psutil.Process(row["pid"])
            while not self.closed.is_set():
                if process_alive(row["pid"], row["process_started"]) is False:
                    break
                try:
                    process.wait(timeout=1)
                    break
                except psutil.TimeoutExpired:
                    continue
            if not self.closed.is_set():
                self.callback(row["run_id"])
        except psutil.NoSuchProcess:
            if not self.closed.is_set():
                self.callback(row["run_id"])
        except psutil.AccessDenied:
            # Lack of permission isn't proof that a worker died.
            return

    def close(self):
        self.closed.set()
