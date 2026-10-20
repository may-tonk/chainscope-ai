"""Bounded operations. Authorization is injected by trusted UI, never model arguments."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from .agent_backend import completion, remaining_work
from .agent_contracts import (
    INPUTS,
    Active,
    AtBlock,
    Budget,
    Fault,
    Issue,
    PlanData,
    Reply,
    StartData,
    StatusData,
    StopData,
)
from .agent_store import RESUMABLE, TERMINAL, now, shown
from .errors import ChainScopeError, SourceUnavailable
from .token_registry import AgentError, Registry


class Service:
    def __init__(
        self,
        store,
        backend,
        owner_id,
        *,
        registry_loader=Registry.load,
        launcher=None,
        approve=None,
    ):
        self.store, self.backend, self.owner_id = store, backend, owner_id
        self.registry_loader, self.launcher, self.approve = (
            registry_loader,
            launcher,
            approve,
        )

    def call(self, tool_name, arguments, operation_id=None, *, authorization=None):
        try:
            model = INPUTS.get(tool_name)
            if model is None:
                raise AgentError("UNKNOWN_TOOL", "没有登记此工具", "none")
            request = model.model_validate(arguments)
            kwargs = (
                {"authorization": authorization}
                if tool_name in {"start_collection", "stop_collection"}
                else {}
            )
            data, warnings = getattr(self, tool_name)(
                request, operation_id or str(uuid4()), **kwargs
            )
            reply = Reply(
                request_id=str(uuid4()),
                observed_at=shown(),
                outcome="partial" if warnings else "success",
                data=data,
                warnings=warnings,
            )
        except ValidationError:
            reply = self.failure(
                "INVALID_INPUT", "工具参数或返回结构不符合合同", "none"
            )
        except AgentError as exc:
            reply = self.failure(exc.code, str(exc), exc.recovery, exc.run_id)
        except SourceUnavailable:
            reply = self.failure(
                "SOURCE_UNAVAILABLE", "来源有限重试后仍不可用", "retry_read"
            )
        except SQLAlchemyError:
            reply = self.failure(
                "DATABASE_UNAVAILABLE",
                "数据库操作未能确认；变更操作先查询状态",
                "check_status",
            )
        except (
            ChainScopeError,
            httpx.HTTPError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ):
            reply = self.failure(
                "OPERATION_FAILED",
                "操作未完成；请检查本地配置、来源和任务状态",
                "check_status",
            )
        return reply.model_dump(mode="json")

    @staticmethod
    def failure(code, message, recovery, run_id=None):
        return Reply(
            request_id=str(uuid4()),
            observed_at=shown(),
            outcome="error",
            data=None,
            error=Fault(
                code=code, message=message, recovery=recovery, related_run_id=run_id
            ),
        )

    def get_collection_status(self, request, _):
        token = self.registry_loader().resolve(request.token_id, enabled=False)
        job, progress = self.backend.inspect(token)
        warnings, attention, active = [], [], None
        if job:
            try:
                self.store.require_ready()
                row = self.store.active(job["job_id"], self.owner_id)
                if row:
                    active = Active(
                        run_id=row["run_id"],
                        state=self.get_run_data(row["run_id"]).state,
                        managed=True,
                    )
            except AgentError as exc:
                warnings.append(Issue(code=exc.code, message=str(exc)))
            if active is None and self.backend.legacy_busy(job["job_id"]):
                active = Active(run_id=None, state="running", managed=False)
            if progress.transfers.quarantined or progress.swaps.failed:
                attention.append(
                    Issue(
                        code="DATA_REVIEW_REQUIRED",
                        message="存在需要处理的失败或隔离窗口",
                    )
                )
        head, head_at = None, None
        if request.reference == "latest_finalized":
            try:
                head, head_at = self.backend.finalized(), shown()
            except (ChainScopeError, httpx.HTTPError, AgentError):
                warnings.append(
                    Issue(
                        code="HEAD_UNAVAILABLE",
                        message="无法取得最终确认高度，本地进度仍可查看",
                    )
                )

        def lag(mark):
            return (
                None
                if head is None or job is None
                else max(
                    0, head - (mark if mark is not None else job["start_block"] - 1)
                )
            )

        return StatusData(
            token=token,
            job_id=job["job_id"] if job else None,
            job_enabled=job["status"] == "active" if job else None,
            progress=progress if job else None,
            reference_head=head,
            head_observed_at=head_at,
            transfer_lag_blocks=lag(progress.transfers_through),
            swap_lag_blocks=lag(progress.swaps_through),
            active_execution=active,
            attention=attention,
        ), warnings

    def prepare_collection(self, request, operation_id):
        registry = self.registry_loader()
        token = registry.resolve(request.token_id)
        self.store.require_ready()
        existing = self.store.by_operation(operation_id, self.owner_id)
        if existing:
            if (
                existing.token.token_id != token.token_id
                or (
                    request.target.mode == "resume"
                    and existing.resume_of_run_id != request.target.run_id
                )
                or (
                    request.target.mode == "block"
                    and existing.target.block != request.target.end_block
                )
                or (
                    request.target.mode == "time"
                    and (
                        existing.target.requested_time is None
                        or datetime.fromisoformat(existing.target.requested_time)
                        != datetime.fromisoformat(request.target.end_time)
                    )
                )
            ):
                raise AgentError(
                    "OPERATION_ALREADY_USED",
                    "同一用户轮次不能把已有计划改为另一个目标",
                    "prepare_again",
                )
            return existing, []
        previous = None
        if request.target.mode == "resume":
            _, previous = self.store.run(request.target.run_id, self.owner_id)
            if previous.token_id != token.token_id or previous.state not in RESUMABLE:
                raise AgentError(
                    "RUN_NOT_RESUMABLE",
                    "运行不属于该代币或当前状态不能续采",
                    "check_status",
                )
            if previous.budget.usage_uncertain:
                raise AgentError(
                    "BUDGET_REVIEW_REQUIRED",
                    "中断用量不确定；请在本地核对并授权新增预算",
                )
            requested = AtBlock(mode="block", end_block=previous.target.block)
        else:
            requested = request.target
        target, job, progress = self.backend.target_and_job(
            token, requested, registry.policy
        )
        if previous:
            if target.block_hash != previous.target.block_hash:
                raise AgentError(
                    "TARGET_HASH_CONFLICT", "原截止区块的哈希发生冲突，不能自动续采"
                )
            target.mode = "resume"
            target.requested_time = previous.target.requested_time
        blockers = self.backend.blockers(job, target.block)
        active = self.store.active(job.job_id, self.owner_id)
        if active:
            blockers.append(
                Issue(code="JOB_BUSY", message="该代币已有活动运行，请先查看运行状态")
            )
        budget = (
            previous.budget
            if previous
            else Budget(
                max_runtime_seconds=registry.policy.max_runtime_seconds,
                max_http_attempts=registry.policy.max_http_attempts,
            )
        )
        if (
            budget.used_runtime_seconds >= budget.max_runtime_seconds
            or budget.used_http_attempts >= budget.max_http_attempts
        ):
            blockers.append(
                Issue(
                    code="BUDGET_EXHAUSTED",
                    message="原授权预算已耗尽，需要本地授权新增预算",
                )
            )
        done = completion(progress, target.block).complete
        plan = PlanData(
            plan_id=str(uuid4()),
            plan_state="blocked" if blockers else "no_work" if done else "ready",
            created_at=shown(),
            expires_at=shown(
                now() + timedelta(seconds=registry.policy.plan_ttl_seconds)
            ),
            token=token,
            job=job,
            target=target,
            baseline=progress,
            work=remaining_work(progress, job.start_block, target.block),
            policy=registry.policy,
            registry_digest=registry.fingerprint(token),
            budget=budget,
            resume_of_run_id=previous.run_id if previous else None,
            blockers=blockers,
        )
        return self.store.save_plan(plan, self.owner_id, operation_id), []

    def start_collection(self, request, _, *, authorization=None):
        self.store.require_ready()
        row, plan = self.store.plan(request.plan_id, self.owner_id)
        if row["run_id"]:
            data = self.get_run_data(row["run_id"])
            return StartData(
                disposition="already_completed"
                if data.state == "completed"
                else "existing_run",
                run=data,
            ), []
        registry = self.registry_loader()
        token = registry.resolve(plan.token.token_id)
        if registry.fingerprint(token) != plan.registry_digest:
            raise AgentError("PLAN_STALE", "服务列表或策略已变化", "prepare_again")
        if plan.plan_state == "no_work":
            return StartData(disposition="no_work", run=None), []
        if plan.plan_state != "ready" or row["expires_at"] <= now():
            raise AgentError("PLAN_STALE", "计划过期或存在阻断问题", "prepare_again")
        if self.launcher is None:
            raise AgentError(
                "EXECUTION_DISABLED", "当前入口仅允许只读检查，不能启动采集", "none"
            )
        if authorization is not None and not authorization("start", plan):
            raise AgentError(
                "AUTHORIZATION_REQUIRED",
                "当前消息没有授权这份采集计划；请明确采集要求，或对刚展示的计划回复‘确认’",
                "none",
            )
        self.backend.validate(plan)
        if plan.authorization_state != "granted":
            if authorization is None and (
                self.approve is None or not self.approve("start", plan)
            ):
                raise AgentError(
                    "AUTHORIZATION_REQUIRED", "未获得本地执行授权，本次没有启动", "none"
                )
            self.store.authorize(
                plan.plan_id,
                self.owner_id,
                plan.plan_digest,
                evidence=getattr(authorization, "evidence", None),
            )
        data, ticket = self.store.reserve(plan.plan_id, self.owner_id)
        if ticket is None:
            return StartData(disposition="existing_run", run=data), []
        try:
            self.launcher(data.run_id, ticket)
        except OSError:
            # No blind retry: a delayed worker must be fenced by trusted recovery first.
            raise AgentError(
                "LAUNCH_UNCONFIRMED",
                "后台启动未确认，请先查询原运行编号",
                "check_status",
                data.run_id,
            ) from None
        return StartData(disposition="accepted", run=data), []

    def get_run_data(self, run_id):
        row, data = self.store.run(run_id, self.owner_id)
        if row["heartbeat_at"]:
            data.last_heartbeat_at = shown(row["heartbeat_at"])
        if row["state"] not in TERMINAL:
            from .agent_worker import process_alive

            reference = row["heartbeat_at"] or row["accepted_at"]
            if not row["pid"] and data.queued_at:
                reference = (
                    datetime.fromisoformat(data.queued_at)
                    .astimezone(UTC)
                    .replace(tzinfo=None)
                )
            stale = now() - reference > timedelta(seconds=60)
            alive = (
                process_alive(row["pid"], row["process_started"])
                if row["pid"]
                else None
            )
            if alive is False or stale:
                data.state, data.recovery = "unknown", "human_action_required"
                data.last_error = Fault(
                    code="WORKER_UNCONFIRMED",
                    message="工作进程状态需要本地核实；不自动创建第二个进程",
                    recovery="human_action_required",
                    related_run_id=run_id,
                )
        return data

    def get_collection_run(self, request, _):
        self.store.require_ready()
        data = self.get_run_data(request.run_id)
        warnings = []
        try:
            _, plan = self.store.plan(data.plan_id, self.owner_id)
            _, fresh = self.backend.inspect(plan.token)
            current_completion = completion(fresh, data.target.block)
            if data.state == "completed" and not current_completion.complete:
                warnings.append(
                    Issue(
                        code="CURRENT_COVERAGE_CHANGED",
                        message="当前任务覆盖已变化；保留此运行完成时的历史快照",
                    )
                )
            else:
                data.progress, data.completion = fresh, current_completion
        except (SQLAlchemyError, ChainScopeError, AgentError, httpx.HTTPError):
            warnings.append(
                Issue(
                    code="STALE_SNAPSHOT",
                    message="无法刷新进度，返回带观测时间的已保存快照",
                )
            )
        if data.state == "unknown":
            warnings.append(
                Issue(code="WORKER_UNCONFIRMED", message="需要在本地恢复入口核实该运行")
            )
        return data, warnings

    def get_collection_event(self, request, _):
        self.store.require_ready()
        return self.store.journal.get(request.event_id, self.owner_id), []

    def list_collection_runs(self, request, _):
        self.store.require_ready()
        return self.store.journal.history(self.owner_id, request), []

    def stop_collection(self, request, _, *, authorization=None):
        self.store.require_ready()
        _, data = self.store.run(request.run_id, self.owner_id)
        if data.state not in TERMINAL and data.stop_requested_at is None:
            approve = authorization if authorization is not None else self.approve
            if approve is None or not approve("stop", data):
                raise AgentError(
                    "AUTHORIZATION_REQUIRED",
                    "未获得本地停止授权，未发送停止请求",
                    "none",
                )
        disposition = self.store.stop(request.run_id, self.owner_id)
        _, data = self.store.run(request.run_id, self.owner_id)
        return StopData(
            run_id=data.run_id,
            disposition=disposition,
            run_state=data.state,
            requested_at=data.stop_requested_at,
            effective_at=data.stop_effective_at,
            safe_stop_pending=data.state not in TERMINAL,
            progress=data.progress,
            resume_target=data.target,
        ), []
