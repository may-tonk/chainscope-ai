"""Transactional control plane, immutable plans and fenced worker startup tickets."""

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from .agent_contracts import Completion, PlanData, RunData
from .agent_models import AGENT_TABLES, attempts, events, plans, runs
from .token_registry import AgentError, digest

TERMINAL = {
    "stopped",
    "paused",
    "completed",
    "needs_attention",
    "failed",
    "interrupted",
}
RESUMABLE = {"stopped", "paused", "interrupted"}


def now():
    return datetime.now(UTC).replace(tzinfo=None)


def shown(value=None):
    from .query import BEIJING

    return (value or now()).replace(tzinfo=UTC).astimezone(BEIJING).isoformat()


def plan_digest(plan):
    return digest(plan.model_dump(exclude={"plan_digest", "authorization_state"}))


class Store:
    def __init__(self, engine):
        self.engine = engine
        from .agent_notifications import EventJournal

        self.journal = EventJournal(engine)

    def require_ready(self):
        with self.engine.connect() as c:
            names = sa.inspect(c).get_table_names()
        if not all(t in names for t in AGENT_TABLES):
            raise AgentError(
                "AGENT_SCHEMA_REQUIRED",
                "Agent 控制表尚未升级；请先审阅并执行 0009 迁移",
            )

    @staticmethod
    def event(c, owner, kind, *, plan_id=None, run_id=None, payload=None):
        c.execute(
            events.insert().values(
                owner_id=owner,
                kind=kind,
                plan_id=plan_id,
                run_id=run_id,
                occurred_at=now(),
                payload=payload or {},
            )
        )

    def owned(self, table, key, identity, owner, *, connection=None, lock=False):
        statement = sa.select(table).where(
            table.c[key] == identity, table.c.owner_id == owner
        )
        if lock:
            statement = statement.with_for_update()
        if connection is not None:
            row = connection.execute(statement).mappings().first()
        else:
            with self.engine.connect() as c:
                row = c.execute(statement).mappings().first()
        if row is None:
            raise AgentError(
                "RECORD_NOT_FOUND", "记录不存在或当前本地身份无权访问", "none"
            )
        return dict(row)

    def plan(self, plan_id, owner, *, connection=None, lock=False):
        row = self.owned(
            plans, "plan_id", plan_id, owner, connection=connection, lock=lock
        )
        plan = PlanData.model_validate(row["payload"])
        calculated = plan_digest(plan)
        if "requested_time" not in row["payload"]["target"]:
            # Older signed plans predate this optional display field. Preserve
            # their original digest without rewriting any saved plan or scope.
            legacy = plan.model_dump(exclude={"plan_digest", "authorization_state"})
            legacy["target"].pop("requested_time", None)
            calculated = digest(legacy)
        if row["digest"] != calculated or plan.plan_digest != row["digest"]:
            raise AgentError("PLAN_TAMPERED", "计划内容与保存的摘要不一致")
        plan.authorization_state = (
            "granted" if row["authorized_digest"] == row["digest"] else "required"
        )
        return row, plan

    def by_operation(self, operation_id, owner):
        with self.engine.connect() as c:
            identity = c.scalar(
                sa.select(plans.c.plan_id).where(
                    plans.c.owner_id == owner,
                    plans.c.operation_id == operation_id,
                )
            )
        return None if identity is None else self.plan(identity, owner)[1]

    def save_plan(self, plan, owner, operation_id):
        plan.plan_digest = plan_digest(plan)
        try:
            with self.engine.begin() as c:
                c.execute(
                    plans.insert().values(
                        plan_id=plan.plan_id,
                        owner_id=owner,
                        operation_id=operation_id,
                        token_id=plan.token.token_id,
                        job_id=plan.job.job_id,
                        digest=plan.plan_digest,
                        payload=plan.model_dump(mode="json"),
                        created_at=now(),
                        expires_at=datetime.fromisoformat(plan.expires_at)
                        .astimezone(UTC)
                        .replace(tzinfo=None),
                    )
                )
                self.event(c, owner, "plan_prepared", plan_id=plan.plan_id)
        except IntegrityError:
            existing = self.by_operation(operation_id, owner)
            if existing is None:
                raise
            return existing
        return plan

    def authorize(self, plan_id, owner, expected_digest, *, evidence=None):
        """Called by trusted local UI only, never exposed as a model tool."""
        with self.engine.begin() as c:
            row, plan = self.plan(plan_id, owner, connection=c, lock=True)
            if (
                plan.plan_state != "ready"
                or row["expires_at"] <= now()
                or row["digest"] != expected_digest
            ):
                raise AgentError(
                    "PLAN_STALE", "计划已失效，请重新准备", "prepare_again"
                )
            c.execute(
                plans.update()
                .where(plans.c.plan_id == plan_id)
                .values(authorized_digest=expected_digest)
            )
            self.event(
                c,
                owner,
                "start_authorized_locally",
                plan_id=plan_id,
                payload={
                    "digest": expected_digest,
                    "authorization": evidence or {"source": "trusted_local_operator"},
                },
            )

    def active(self, job_id, owner):
        with self.engine.connect() as c:
            row = (
                c.execute(sa.select(runs).where(runs.c.active_job == job_id))
                .mappings()
                .first()
            )
        if row is None:
            return None
        if row["owner_id"] != owner:
            raise AgentError(
                "JOB_BUSY", "该代币已有其他本地身份的活动运行", "check_status"
            )
        return dict(row)

    def run(self, run_id, owner):
        row = self.owned(runs, "run_id", run_id, owner)
        data = RunData.model_validate(row["payload"])
        if row["stop_requested_at"]:
            data.stop_requested_at = shown(row["stop_requested_at"])
            if data.state not in TERMINAL:
                data.state = "stop_requested"
        return row, data

    def reserve(self, plan_id, owner):
        """Atomically bind a plan to one logical run and one launch ticket."""
        try:
            with self.engine.begin() as c:
                row, plan = self.plan(plan_id, owner, connection=c, lock=True)
                if row["run_id"]:
                    current = self.owned(
                        runs, "run_id", row["run_id"], owner, connection=c
                    )
                    return RunData.model_validate(current["payload"]), None
                if row["expires_at"] <= now() or plan.plan_state != "ready":
                    raise AgentError(
                        "PLAN_STALE", "计划过期或不可启动", "prepare_again"
                    )
                if row["authorized_digest"] != row["digest"]:
                    raise AgentError(
                        "AUTHORIZATION_REQUIRED", "需要在本地入口授权此份具体计划"
                    )
                ticket = str(uuid4())
                if plan.resume_of_run_id:
                    current = self.owned(
                        runs,
                        "run_id",
                        plan.resume_of_run_id,
                        owner,
                        connection=c,
                        lock=True,
                    )
                    data = RunData.model_validate(current["payload"])
                    if current["state"] not in RESUMABLE:
                        raise AgentError(
                            "RUN_NOT_RESUMABLE", "当前运行状态不能续采", "check_status"
                        )
                    if (
                        data.target.block != plan.target.block
                        or data.target.block_hash != plan.target.block_hash
                    ):
                        raise AgentError("TARGET_CHANGED", "续采计划不能修改原目标")
                    # A newer budget grant invalidates an older prepared resume plan.
                    if data.budget != plan.budget:
                        raise AgentError(
                            "PLAN_STALE",
                            "原运行预算已变化，请重新准备",
                            "prepare_again",
                        )
                    if data.budget.usage_uncertain:
                        raise AgentError(
                            "BUDGET_REVIEW_REQUIRED",
                            "上次中断用量不确定，需要本地核对并授权新增预算",
                        )
                    data.plan_id = plan_id
                    data.state, data.phase = "queued", "queued"
                    data.queued_at = shown()
                    data.ended_at = data.stop_requested_at = data.stop_effective_at = (
                        None
                    )
                    data.last_error = None
                    c.execute(
                        runs.update()
                        .where(runs.c.run_id == data.run_id)
                        .values(
                            active_job=plan.job.job_id,
                            state="queued",
                            payload=data.model_dump(mode="json"),
                            ticket=ticket,
                            pid=None,
                            process_started=None,
                            heartbeat_at=None,
                            stop_requested_at=None,
                        )
                    )
                else:
                    data = RunData(
                        run_id=str(uuid4()),
                        plan_id=plan_id,
                        token_id=plan.token.token_id,
                        job_id=plan.job.job_id,
                        state="queued",
                        phase="queued",
                        target=plan.target,
                        accepted_at=shown(),
                        queued_at=shown(),
                        budget=plan.budget,
                        completion=Completion(
                            transfers=False, swaps=False, pools=False, balances=False
                        ),
                        progress=plan.baseline,
                        next_poll_after_seconds=plan.policy.poll_interval_seconds,
                    )
                    c.execute(
                        runs.insert().values(
                            run_id=data.run_id,
                            owner_id=owner,
                            token_id=data.token_id,
                            job_id=data.job_id,
                            active_job=data.job_id,
                            state="queued",
                            payload=data.model_dump(mode="json"),
                            ticket=ticket,
                            accepted_at=now(),
                        )
                    )
                c.execute(
                    plans.update()
                    .where(plans.c.plan_id == plan_id)
                    .values(run_id=data.run_id)
                )
                self.event(
                    c, owner, "launch_reserved", plan_id=plan_id, run_id=data.run_id
                )
                return data, ticket
        except IntegrityError:
            raise AgentError(
                "JOB_BUSY", "同一代币已有活动运行，不重复启动", "check_status"
            ) from None

    def claim(self, run_id, ticket, pid, process_started):
        with self.engine.begin() as c:
            row = (
                c.execute(
                    sa.select(runs).where(runs.c.run_id == run_id).with_for_update()
                )
                .mappings()
                .one()
            )
            if row["ticket"] != ticket or row["state"] != "queued":
                return None
            data = RunData.model_validate(row["payload"])
            data.state, data.phase = "starting", "preflight"
            data.started_at = data.started_at or shown()
            data.attempt_count += 1
            data.last_heartbeat_at = shown()
            attempt_id = str(uuid4())
            c.execute(
                runs.update()
                .where(runs.c.run_id == run_id)
                .values(
                    pid=pid,
                    process_started=process_started,
                    state="starting",
                    heartbeat_at=now(),
                    payload=data.model_dump(mode="json"),
                )
            )
            c.execute(
                attempts.insert().values(
                    attempt_id=attempt_id,
                    run_id=run_id,
                    ticket=ticket,
                    state="starting",
                    started_at=now(),
                    payload={},
                )
            )
            self.event(c, row["owner_id"], "worker_claimed", run_id=run_id)
            claimed = dict(row), data, attempt_id
        from .agent_notifications import wake

        wake(row["owner_id"], run_id)
        return claimed

    def save_run(self, run_id, ticket, data, *, attempt_id=None, final=False):
        with self.engine.begin() as c:
            row = (
                c.execute(
                    sa.select(runs).where(runs.c.run_id == run_id).with_for_update()
                )
                .mappings()
                .one()
            )
            if row["ticket"] != ticket:
                raise AgentError("WORKER_FENCED", "该执行尝试已失效，停止处理")
            if row["state"] in TERMINAL:
                if final:
                    return self.journal.finish(
                        c,
                        row,
                        RunData.model_validate(row["payload"]),
                        attempt_id=attempt_id,
                    )
                raise AgentError("WORKER_FENCED", "该执行尝试已经结束")
            if final and data.state not in TERMINAL:
                raise AgentError("INVALID_FINAL_STATE", "结束记录必须使用终止状态")
            # Ticket is a fence for delayed/duplicate worker startup and final writes.
            count = c.execute(
                runs.update()
                .where(
                    runs.c.run_id == run_id,
                    runs.c.ticket == ticket,
                )
                .values(
                    payload=data.model_dump(mode="json"),
                    state=data.state,
                    heartbeat_at=now(),
                    **({"active_job": None} if final else {}),
                )
            ).rowcount
            if count != 1:
                raise AgentError("WORKER_FENCED", "该执行尝试已失效，停止处理")
            if attempt_id:
                c.execute(
                    attempts.update()
                    .where(
                        attempts.c.attempt_id == attempt_id,
                        attempts.c.run_id == run_id,
                        attempts.c.ticket == ticket,
                    )
                    .values(
                        state=data.state,
                        payload={
                            "budget": data.budget.model_dump(),
                            "last_error": data.last_error.model_dump()
                            if data.last_error
                            else None,
                        },
                        **({"ended_at": now()} if final else {}),
                    )
                )
            event_id = (
                self.journal.finish(c, row, data, attempt_id=attempt_id)
                if final
                else None
            )
        if final:
            from .agent_notifications import wake

            wake(row["owner_id"], run_id)
        return event_id

    def heartbeat(self, run_id, ticket):
        with self.engine.begin() as c:
            count = c.execute(
                runs.update()
                .where(
                    runs.c.run_id == run_id,
                    runs.c.ticket == ticket,
                    runs.c.active_job.is_not(None),
                )
                .values(heartbeat_at=now())
            ).rowcount
            return count == 1

    def should_stop(self, run_id, ticket):
        with self.engine.connect() as c:
            row = c.execute(
                sa.select(runs.c.ticket, runs.c.stop_requested_at).where(
                    runs.c.run_id == run_id
                )
            ).first()
        return row is None or row[0] != ticket or row[1] is not None

    def stop(self, run_id, owner):
        with self.engine.begin() as c:
            row = self.owned(runs, "run_id", run_id, owner, connection=c, lock=True)
            if row["state"] in TERMINAL:
                return "already_terminal"
            if row["stop_requested_at"]:
                return "already_requested"
            values = {"stop_requested_at": now()}
            if row["state"] == "queued" and row["pid"] is None:
                data = RunData.model_validate(row["payload"])
                data.state, data.phase = "stopped", "stopped_before_start"
                data.stop_requested_at = data.stop_effective_at = data.ended_at = (
                    shown()
                )
                data.recovery, data.next_poll_after_seconds = "resume_same_target", None
                values.update(
                    state="stopped",
                    active_job=None,
                    ticket=str(uuid4()),
                    payload=data.model_dump(mode="json"),
                )
            c.execute(runs.update().where(runs.c.run_id == run_id).values(**values))
            if row["state"] == "queued" and row["pid"] is None:
                self.journal.finish(c, row, data)
            self.event(c, owner, "stop_authorized_locally", run_id=run_id)
        from .agent_notifications import wake

        wake(owner, run_id)
        return "requested"

    def recover_dead(self, run_id, owner, process_alive):
        """Trusted recovery: invalidate unclaimed launches; never replace a live worker."""
        with self.engine.begin() as c:
            row = self.owned(runs, "run_id", run_id, owner, connection=c, lock=True)
            if row["state"] in TERMINAL:
                return
            if (
                row["pid"] is not None
                and process_alive(row["pid"], row["process_started"]) is not False
            ):
                raise AgentError(
                    "WORKER_NOT_CONFIRMED_DEAD", "无法确认旧工作进程已结束，不能接管"
                )
            data = RunData.model_validate(row["payload"])
            data.state, data.phase, data.ended_at = "interrupted", "recovered", shown()
            data.budget.usage_uncertain = row["pid"] is not None
            data.recovery = (
                "human_action_required"
                if data.budget.usage_uncertain
                else "resume_same_target"
            )
            data.next_poll_after_seconds = None
            c.execute(
                runs.update()
                .where(runs.c.run_id == run_id)
                .values(
                    ticket=str(uuid4()),
                    active_job=None,
                    state=data.state,
                    payload=data.model_dump(mode="json"),
                )
            )
            c.execute(
                attempts.update()
                .where(attempts.c.run_id == run_id, attempts.c.ended_at.is_(None))
                .values(
                    ended_at=now(),
                    state="interrupted",
                    payload={"usage_uncertain": data.budget.usage_uncertain},
                )
            )
            self.event(c, owner, "dead_execution_recovered", run_id=run_id)
            self.journal.finish(c, row, data)
        from .agent_notifications import wake

        wake(owner, run_id)

    def grant_extra_budget(self, run_id, owner, seconds, requests):
        """Explicit local operator action only; preserves all prior recorded usage."""
        if (
            type(seconds) is not int
            or type(requests) is not int
            or not 1 <= seconds <= 86400
            or not 1 <= requests <= 1000000
        ):
            raise AgentError("INVALID_INPUT", "新增预算必须是范围内的正整数")
        with self.engine.begin() as c:
            row = self.owned(runs, "run_id", run_id, owner, connection=c, lock=True)
            if row["state"] not in RESUMABLE:
                raise AgentError(
                    "RUN_NOT_RESUMABLE", "只能为已停止、暂停或确认中断的运行增加预算"
                )
            data = RunData.model_validate(row["payload"])
            if data.budget.usage_uncertain:
                # Charge the old allowance conservatively, instead of resetting unknown use to zero.
                data.budget.used_http_attempts = max(
                    data.budget.used_http_attempts, data.budget.max_http_attempts
                )
                data.budget.used_runtime_seconds = max(
                    data.budget.used_runtime_seconds,
                    float(data.budget.max_runtime_seconds),
                )
            data.budget.max_runtime_seconds = (
                int(
                    max(
                        data.budget.max_runtime_seconds,
                        data.budget.used_runtime_seconds,
                    )
                )
                + seconds
            )
            data.budget.max_http_attempts = (
                max(data.budget.max_http_attempts, data.budget.used_http_attempts)
                + requests
            )
            data.budget.usage_uncertain = False
            c.execute(
                runs.update()
                .where(runs.c.run_id == run_id)
                .values(payload=data.model_dump(mode="json"))
            )
            self.event(
                c,
                owner,
                "extra_budget_authorized_locally",
                run_id=run_id,
                payload={"seconds": seconds, "requests": requests},
            )
