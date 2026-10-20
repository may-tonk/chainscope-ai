"""Versioned tool contracts. The same models validate local inputs and export schemas."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

UUIDText = Annotated[str, Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
TokenID = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
Block = Annotated[int, Field(ge=0, le=2**63 - 1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Token(Contract):
    token_id: TokenID
    chain_id: Literal[56]
    address: Annotated[str, Field(pattern=r"^0x[0-9a-f]{40}$")]
    symbol: Annotated[str, Field(min_length=1, max_length=32)]
    enabled: bool
    profile: Literal["alchemy_sqd_v1"]


class Policy(Contract):
    max_runtime_seconds: Annotated[int, Field(ge=1, le=86400)] = 1800
    max_http_attempts: Annotated[int, Field(ge=1, le=1000000)] = 1000
    plan_ttl_seconds: Annotated[int, Field(ge=60, le=86400)] = 900
    poll_interval_seconds: Annotated[int, Field(ge=5, le=300)] = 15
    window_size: Annotated[int, Field(ge=1, le=10000000)] = 50000


class Latest(Contract):
    mode: Literal["latest_finalized"]


class AtBlock(Contract):
    mode: Literal["block"]
    end_block: Block


class AtTime(Contract):
    mode: Literal["time"]
    end_time: Annotated[str, Field(max_length=40)]

    @model_validator(mode="after")
    def explicit_timezone(self):
        if datetime.fromisoformat(self.end_time).utcoffset() is None:
            raise ValueError("end_time requires an explicit timezone")
        return self


class Resume(Contract):
    mode: Literal["resume"]
    run_id: UUIDText


class StatusInput(Contract):
    token_id: TokenID
    reference: Literal["local", "latest_finalized"]


class PrepareInput(Contract):
    token_id: TokenID
    target: Annotated[Latest | AtBlock | AtTime | Resume, Field(discriminator="mode")]


class StartInput(Contract):
    plan_id: UUIDText


class RunInput(Contract):
    run_id: UUIDText


class StopInput(RunInput):
    pass


class Windows(Contract):
    done: int = 0
    pending: int = 0
    running: int = 0
    failed: int = 0
    quarantined: int = 0


class Progress(Contract):
    collected_through: Block | None = None
    transfers_through: Block | None = None
    swaps_through: Block | None = None
    pools_through: Block | None = None
    balances_through: Block | None = None
    transfers: Windows = Field(default_factory=Windows)
    swaps: Windows = Field(default_factory=Windows)
    transfer_events: int | None = None
    swap_events: int | None = None
    pending_events: int | None = None
    observed_at: str


class Issue(Contract):
    code: str
    message: str


class Fault(Issue):
    recovery: Literal[
        "retry_read",
        "check_status",
        "prepare_again",
        "resume_same_target",
        "human_action_required",
        "none",
    ] = "none"
    related_run_id: UUIDText | None = None


class Target(Contract):
    block: Block
    block_hash: Annotated[str, Field(pattern=r"^0x[0-9a-f]{64}$")]
    time_beijing: str
    mode: Literal["latest_finalized", "block", "time", "resume"]
    requested_time: str | None = None


class JobSpec(Contract):
    job_id: TokenID
    start_block: Block
    deploy_block: Block
    decimals: Annotated[int, Field(ge=0, le=255)]
    create_required: bool
    window_size: int
    parser_version: int


class Budget(Contract):
    scope: Literal["worker_http_attempts"] = "worker_http_attempts"
    max_runtime_seconds: int
    max_http_attempts: int
    used_runtime_seconds: float = 0.0
    used_http_attempts: int = 0
    usage_uncertain: bool = False
    soft_limits: Literal[True] = True


class Completion(Contract):
    transfers: bool
    swaps: bool
    pools: bool
    balances: bool

    @property
    def complete(self):
        return all((self.transfers, self.swaps, self.pools, self.balances))


class Active(Contract):
    run_id: UUIDText | None
    state: str
    managed: bool


class StatusData(Contract):
    kind: Literal["status"] = "status"
    token: Token
    job_id: str | None
    job_enabled: bool | None
    progress: Progress | None
    reference_head: Block | None
    head_observed_at: str | None
    transfer_lag_blocks: int | None
    swap_lag_blocks: int | None
    active_execution: Active | None
    attention: list[Issue]
    count_scope: Literal["job_done_windows"] = "job_done_windows"


class Work(Contract):
    transfers_from: Block | None
    swaps_from: Block | None
    pools_from: Block | None
    balances_from: Block | None


class PlanData(Contract):
    kind: Literal["plan"] = "plan"
    plan_id: UUIDText
    plan_state: Literal["ready", "no_work", "blocked"]
    created_at: str
    expires_at: str
    token: Token
    job: JobSpec
    target: Target
    baseline: Progress
    work: Work
    policy: Policy
    registry_digest: str
    budget: Budget
    resume_of_run_id: UUIDText | None
    authorization_state: Literal["required", "granted"] = "required"
    blockers: list[Issue]
    plan_digest: str = ""


RunState = Literal[
    "queued",
    "starting",
    "running",
    "stop_requested",
    "stopped",
    "paused",
    "completed",
    "needs_attention",
    "failed",
    "interrupted",
    "unknown",
]


class Counters(Contract):
    inserted_transfers: int | None = None
    transfer_windows: int | None = None
    swap_windows: int | None = None
    inserted_swaps: int | None = None


class RunData(Contract):
    kind: Literal["run"] = "run"
    run_id: UUIDText
    plan_id: UUIDText
    token_id: TokenID
    job_id: TokenID
    state: RunState
    phase: str
    target: Target
    accepted_at: str
    queued_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    last_heartbeat_at: str | None = None
    progress: Progress | None = None
    budget: Budget
    completion: Completion
    run_counters: Counters = Field(default_factory=Counters)
    stop_requested_at: str | None = None
    stop_effective_at: str | None = None
    last_error: Fault | None = None
    recovery: str = "check_status"
    attempt_count: int = 0
    next_poll_after_seconds: int | None = 15

    @model_validator(mode="after")
    def completed_is_complete(self):
        if self.state == "completed" and not self.completion.complete:
            raise ValueError("completed requires all four coverage components")
        return self


class StartData(Contract):
    kind: Literal["start"] = "start"
    disposition: Literal["accepted", "existing_run", "already_completed", "no_work"]
    run: RunData | None


class StopData(Contract):
    kind: Literal["stop"] = "stop"
    run_id: UUIDText
    disposition: Literal["requested", "already_requested", "already_terminal"]
    run_state: RunState
    requested_at: str | None
    effective_at: str | None
    safe_stop_pending: bool
    progress: Progress | None
    resume_target: Target


class EventInput(Contract):
    event_id: Annotated[int, Field(ge=1)]


class HistoryInput(Contract):
    token_id: TokenID | None = None
    created_from: Annotated[str, Field(max_length=40)] | None = None
    created_to: Annotated[str, Field(max_length=40)] | None = None
    before_run_id: UUIDText | None = None
    limit: Annotated[int, Field(ge=1, le=50)] = 20

    @model_validator(mode="after")
    def dated_range(self):
        bounds = []
        for value in (self.created_from, self.created_to):
            parsed = datetime.fromisoformat(value) if value else None
            if parsed is not None and parsed.utcoffset() is None:
                raise ValueError("history dates require an explicit timezone")
            bounds.append(parsed)
        if all(v is not None for v in bounds) and bounds[0] >= bounds[1]:
            raise ValueError("created_from must precede created_to")
        return self


class Delivery(Contract):
    state: Literal["pending", "generating", "generated", "displayed", "failed"]
    text: str | None = None
    generated_at: str | None = None
    displayed_at: str | None = None
    generation_attempts: int = 0


class EventData(Contract):
    kind: Literal["event"] = "event"
    event_id: Annotated[int, Field(ge=1)]
    event_type: Literal["run_finished", "run_unconfirmed"]
    occurred_at: str
    attempt_id: UUIDText | None
    attempt_started_at: str | None
    token: Token
    run: RunData
    report: Delivery
    result_scope: Literal["immutable_event_snapshot"] = "immutable_event_snapshot"


class RunSummary(Contract):
    run_id: UUIDText
    token_id: TokenID
    state: RunState
    accepted_at: str
    ended_at: str | None
    target: Target
    latest_event_id: int | None


class HistoryData(Contract):
    kind: Literal["history"] = "history"
    items: list[RunSummary]
    next_cursor: UUIDText | None
    state_scope: Literal["last_saved_state_use_get_collection_run_for_live_status"] = (
        "last_saved_state_use_get_collection_run_for_live_status"
    )
    time_filter: Literal["accepted_at_from_inclusive_to_exclusive"] = (
        "accepted_at_from_inclusive_to_exclusive"
    )


Data = Annotated[
    StatusData | PlanData | RunData | StartData | StopData | EventData | HistoryData,
    Field(discriminator="kind"),
]


class Reply(Contract):
    contract_version: Literal["1.0"] = "1.0"
    request_id: UUIDText
    outcome: Literal["success", "partial", "error"]
    observed_at: str
    data: Data | None
    warnings: list[Issue] = Field(default_factory=list)
    error: Fault | None = None

    @model_validator(mode="after")
    def error_matches_outcome(self):
        if (self.outcome == "error") != (self.error is not None):
            raise ValueError(
                "error outcome requires an error; others must use warnings"
            )
        return self


INPUTS = {
    "get_collection_status": StatusInput,
    "prepare_collection": PrepareInput,
    "start_collection": StartInput,
    "get_collection_run": RunInput,
    "stop_collection": StopInput,
    "get_collection_event": EventInput,
    "list_collection_runs": HistoryInput,
}
