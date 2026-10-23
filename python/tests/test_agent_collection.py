"""Tool contract and control-plane failure tests; no live source or formal database."""

import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from agents import RunConfig, Runner
from openai import AsyncOpenAI
from pydantic import ValidationError

from chainscope.agent_contracts import (
    INPUTS,
    AtBlock,
    Completion,
    JobSpec,
    Progress,
    Reply,
    RunData,
    Target,
)
from chainscope.agent_models import attempts, events, plans, runs
from chainscope.agent_runtime import build_agent, local_owner
from chainscope.agent_service import Service
from chainscope.agent_store import Store, now, shown
from chainscope.agent_worker import execute_worker
from chainscope.errors import SourceUnavailable
from chainscope.token_registry import AgentError, Registry

OWNER = str(uuid4())


class FakeBackend:
    def __init__(self):
        self.progress = Progress(
            observed_at=shown(), transfer_events=0, swap_events=0, pending_events=0
        )
        self.head = 20
        self.busy = False
        self.blocked = []
        self.finish = "complete"
        self.checked = 0
        self.expected_hash = "0x" + "a" * 64

    def inspect(self, token):
        return {
            "job_id": token.token_id,
            "start_block": 1,
            "status": "active",
        }, self.progress

    def finalized(self):
        if self.head is None:
            raise AgentError("HEAD_UNAVAILABLE", "unavailable")
        return self.head

    def legacy_busy(self, job_id):
        return self.busy

    def target_and_job(self, token, requested, policy):
        end = self.head if requested.mode == "latest_finalized" else requested.end_block
        return (
            Target(
                block=end,
                block_hash=self.expected_hash,
                time_beijing=shown(),
                mode=requested.mode,
            ),
            JobSpec(
                job_id=token.token_id,
                start_block=1,
                deploy_block=1,
                decimals=8,
                create_required=False,
                window_size=50000,
                parser_version=1,
            ),
            self.progress,
        )

    def blockers(self, job, target):
        return self.blocked

    def validate(self, plan):
        self.checked += 1
        return self.progress

    def execute(self, plan, notify, stop, meter):
        meter["read"] = lambda: 9
        if self.finish == "crash":
            raise RuntimeError("secret-api-key must not leak")
        if self.finish == "mismatch":
            notify(json.dumps({"quarantined": [1, 20]}))
            return {"stopped": "stop_requested", "quarantined_remaining": 1}
        if self.finish == "stop":
            return {"stopped": "max_runtime"}
        if self.finish == "source_down":
            raise SourceUnavailable("primary source quota exhausted")
        self.progress = Progress(
            observed_at=shown(),
            collected_through=20,
            transfers_through=20,
            swaps_through=20,
            pools_through=20,
            balances_through=20,
            transfer_events=12,
            swap_events=3,
            pending_events=0,
        )
        notify(json.dumps({"phase": "done"}))
        return {
            "stopped": "caught_up",
            "inserted": 12,
            "windows_done": 1,
            "swap_windows_done": 1,
        }


@pytest.fixture
def control(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'control.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    for table in (plans, runs, attempts, events):
        table.create(engine)
    store = Store(engine)
    backend = FakeBackend()
    launches = []
    service = Service(
        store,
        backend,
        OWNER,
        launcher=lambda *args: launches.append(args),
        approve=lambda *_: True,
    )
    yield service, launches
    engine.dispose()


def prepare(service, mode="latest_finalized", **extra):
    result = service.call(
        "prepare_collection", {"token_id": "bem", "target": {"mode": mode, **extra}}
    )
    assert result["outcome"] == "success", result
    return result["data"]


def start(service, plan):
    result = service.call("start_collection", {"plan_id": plan["plan_id"]})
    assert result["outcome"] == "success", result
    return result["data"]["run"]


@pytest.mark.parametrize(
    "args",
    [
        {"token_id": "bem", "reference": "local", "confirmed": True},
        {"token_id": 1, "reference": "local"},
        {"token_id": "bem"},
    ],
)
def test_reject_invalid_status(control, args):
    service, launches = control
    assert (
        service.call("get_collection_status", args)["error"]["code"] == "INVALID_INPUT"
    )
    assert not launches


@pytest.mark.parametrize("value", ["20", 20.5, True, -1])
def test_block_is_strict_integer(value):
    with pytest.raises(ValidationError):
        AtBlock(mode="block", end_block=value)


def test_unknown_token_never_touches_database_or_source(control):
    service, _ = control
    service.store.require_ready = lambda: pytest.fail("DB accessed before admission")
    service.backend.inspect = lambda _: pytest.fail("source accessed before admission")
    assert (
        service.call(
            "prepare_collection",
            {"token_id": "unknown", "target": {"mode": "latest_finalized"}},
        )["error"]["code"]
        == "TOKEN_NOT_SUPPORTED"
    )


def test_registry_supports_multiple_tokens_and_disabled_read(control):
    service, _ = control
    registry = Registry.load()
    registry.tokens.append(
        registry.tokens[0].model_copy(
            update={"token_id": "other", "address": "0x" + "1" * 40, "symbol": "OTHER"}
        )
    )
    service.registry_loader = lambda: registry
    result = service.call(
        "prepare_collection",
        {"token_id": "other", "target": {"mode": "latest_finalized"}},
    )
    assert result["data"]["token"]["address"] == "0x" + "1" * 40
    registry.tokens[1].enabled = False
    assert (
        service.call(
            "prepare_collection",
            {"token_id": "other", "target": {"mode": "latest_finalized"}},
        )["error"]["code"]
        == "TOKEN_DISABLED"
    )
    assert (
        service.call(
            "get_collection_status", {"token_id": "other", "reference": "local"}
        )["data"]["token"]["enabled"]
        is False
    )


def test_head_failure_is_partial_not_zero(control):
    service, _ = control
    service.backend.head = None
    result = service.call(
        "get_collection_status", {"token_id": "bem", "reference": "latest_finalized"}
    )
    assert result["outcome"] == "partial"
    assert result["data"]["reference_head"] is None
    assert result["data"]["transfer_lag_blocks"] is None


def test_prepare_only_writes_plan_and_reuses_operation(control):
    service, launches = control
    op = str(uuid4())
    args = {"token_id": "bem", "target": {"mode": "latest_finalized"}}
    one = service.call("prepare_collection", args, op)
    service.backend.head = 30
    two = service.call("prepare_collection", args, op)
    assert one["data"] == two["data"]
    assert one["data"]["target"]["block"] == 20
    assert not launches
    with service.store.engine.connect() as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(runs)) == 0


def test_start_idempotent_and_lost_response(control):
    service, launches = control
    plan = prepare(service)
    one, two = start(service, plan), start(service, plan)
    assert one["run_id"] == two["run_id"]
    assert len(launches) == 1


def test_start_requires_real_local_authorization(control):
    service, launches = control
    service.approve = None
    plan = prepare(service)
    result = service.call("start_collection", {"plan_id": plan["plan_id"]})
    assert result["error"]["code"] == "AUTHORIZATION_REQUIRED"
    assert not launches
    forged = service.call(
        "start_collection", {"plan_id": plan["plan_id"], "confirmed": True}
    )
    assert forged["error"]["code"] == "INVALID_INPUT"


def test_expiry_and_tampering(control):
    service, launches = control
    plan = prepare(service)
    with service.store.engine.begin() as c:
        c.execute(plans.update().values(expires_at=now() - timedelta(seconds=1)))
    assert (
        service.call("start_collection", {"plan_id": plan["plan_id"]})["error"]["code"]
        == "PLAN_STALE"
    )
    with service.store.engine.begin() as c:
        payload = dict(plan)
        payload["target"] = {**payload["target"], "block": 21}
        c.execute(plans.update().values(payload=payload))
    assert (
        service.call("start_collection", {"plan_id": plan["plan_id"]})["error"]["code"]
        == "PLAN_TAMPERED"
    )
    assert not launches


def test_registry_change_invalidates_plan(control):
    service, launches = control
    plan = prepare(service)
    changed = Registry.load()
    changed.policy.window_size += 1
    service.registry_loader = lambda: changed
    assert (
        service.call("start_collection", {"plan_id": plan["plan_id"]})["error"]["code"]
        == "PLAN_STALE"
    )
    assert not launches


def test_two_plans_cannot_start_same_job(control):
    service, launches = control
    a, b = prepare(service), prepare(service)
    start(service, a)
    assert (
        service.call("start_collection", {"plan_id": b["plan_id"]})["error"]["code"]
        == "JOB_BUSY"
    )
    assert len(launches) == 1


def test_another_owner_cannot_read_or_stop(control):
    service, _ = control
    data = start(service, prepare(service))
    stranger = Service(
        service.store, service.backend, str(uuid4()), approve=lambda *_: True
    )
    for name in ("get_collection_run", "stop_collection"):
        assert (
            stranger.call(name, {"run_id": data["run_id"]})["error"]["code"]
            == "RECORD_NOT_FOUND"
        )


def test_stop_before_worker_claim_fences_delayed_launch(control):
    service, launches = control
    data = start(service, prepare(service))
    result = service.call("stop_collection", {"run_id": data["run_id"]})
    assert result["data"]["run_state"] == "stopped"
    assert result["data"]["safe_stop_pending"] is False
    assert service.store.claim(*launches[0], 999, 1.0) is None
    assert (
        service.call("stop_collection", {"run_id": data["run_id"]})["data"][
            "disposition"
        ]
        == "already_terminal"
    )


def test_stop_active_worker_is_async_and_idempotent(control):
    service, launches = control
    data = start(service, prepare(service))
    service.store.claim(*launches[0], 999, 1.0)
    first = service.call("stop_collection", {"run_id": data["run_id"]})["data"]
    second = service.call("stop_collection", {"run_id": data["run_id"]})["data"]
    assert first["run_state"] == "stop_requested" and first["safe_stop_pending"]
    assert second["disposition"] == "already_requested"
    assert first["effective_at"] is None


def test_resume_keeps_target_and_run_identity(control):
    service, launches = control
    data = start(service, prepare(service))
    service.call("stop_collection", {"run_id": data["run_id"]})
    service.backend.head = 99
    plan = prepare(service, "resume", run_id=data["run_id"])
    resumed = start(service, plan)
    assert resumed["target"]["block"] == 20
    assert resumed["run_id"] == data["run_id"]
    assert launches[0][1] != launches[1][1]


@pytest.mark.parametrize(
    "finish, expected",
    [
        ("complete", "completed"),
        ("stop", "paused"),
        ("mismatch", "needs_attention"),
        ("crash", "failed"),
    ],
)
def test_worker_states_and_secret_redaction(control, finish, expected):
    service, launches = control
    service.backend.finish = finish
    data = start(service, prepare(service))
    assert execute_worker(service.store, service.backend, *launches[0])
    _, saved = service.store.run(data["run_id"], OWNER)
    assert saved.state == expected
    assert "secret-api-key" not in saved.model_dump_json()
    assert service.store.active("bem", OWNER) is None
    assert not execute_worker(service.store, service.backend, *launches[0])
    assert saved.budget.used_http_attempts == 9


@pytest.mark.parametrize(
    "first_attempt, inserted", [("stop", 12), ("source_down", None)]
)
def test_run_counters_stay_unknown_after_an_uncounted_attempt(
    control, first_attempt, inserted
):
    service, launches = control
    service.backend.finish = first_attempt
    data = start(service, prepare(service))
    execute_worker(service.store, service.backend, *launches[0])
    _, paused = service.store.run(data["run_id"], OWNER)
    assert paused.state == "paused"
    service.backend.finish = "complete"
    start(service, prepare(service, "resume", run_id=data["run_id"]))
    execute_worker(service.store, service.backend, *launches[1])
    _, done = service.store.run(data["run_id"], OWNER)
    assert done.state == "completed" and done.attempt_count == 2
    # A failed attempt's inserts were never counted, so the run total must
    # stay unknown instead of showing only the last attempt's 12 as exact.
    assert done.run_counters.inserted_transfers == inserted
    assert done.run_counters.transfer_windows == (1 if inserted else None)


def test_recovery_never_takes_live_worker_and_unknown_budget_is_not_reset(control):
    service, launches = control
    data = start(service, prepare(service))
    service.store.claim(*launches[0], 999, 1.0)
    with pytest.raises(AgentError, match="无法确认"):
        service.store.recover_dead(data["run_id"], OWNER, lambda *_: True)
    service.store.recover_dead(data["run_id"], OWNER, lambda *_: False)
    result = service.call(
        "prepare_collection",
        {"token_id": "bem", "target": {"mode": "resume", "run_id": data["run_id"]}},
    )
    assert result["error"]["code"] == "BUDGET_REVIEW_REQUIRED"
    service.store.grant_extra_budget(data["run_id"], OWNER, 100, 100)
    plan = prepare(service, "resume", run_id=data["run_id"])
    assert plan["budget"]["used_http_attempts"] == 1000
    assert plan["budget"]["max_http_attempts"] == 1100


def test_unclaimed_launch_recovery_fences_old_ticket(control):
    service, launches = control
    data = start(service, prepare(service))
    service.store.recover_dead(data["run_id"], OWNER, lambda *_: pytest.fail("no pid"))
    assert service.store.claim(*launches[0], 999, 1.0) is None
    plan = prepare(service, "resume", run_id=data["run_id"])
    assert plan["budget"]["usage_uncertain"] is False


def test_completed_requires_all_coverage():
    with pytest.raises(ValidationError):
        RunData(
            run_id=str(uuid4()),
            plan_id=str(uuid4()),
            token_id="bem",
            job_id="bem",
            state="completed",
            phase="done",
            target=Target(
                block=20, block_hash="0x" + "a" * 64, time_beijing=shown(), mode="block"
            ),
            accepted_at=shown(),
            budget={"max_runtime_seconds": 30, "max_http_attempts": 100},
            completion=Completion(
                transfers=True, swaps=False, pools=True, balances=True
            ),
        )


def test_schemas_and_replies_share_types(control):
    service, _ = control
    assert len(INPUTS) == 7
    for model in INPUTS.values():
        schema = model.model_json_schema()
        assert schema["additionalProperties"] is False
        assert set(schema.get("required", [])) <= set(schema["properties"])
    Reply.model_validate(
        service.call("get_collection_status", {"token_id": "bem", "reference": "local"})
    )


def test_readonly_context_blocks_every_mutating_tool(control):
    from types import SimpleNamespace

    from chainscope.agent_tools import ToolContext, make_tools

    service, launches = control
    context = SimpleNamespace(
        context=ToolContext(service, str(uuid4()), read_only=True)
    )
    for tool in make_tools():
        if tool.name == "get_collection_status":
            continue
        result = json.loads(asyncio.run(tool.on_invoke_tool(context, "{}")))
        assert result["error"]["code"] == "EXECUTION_DISABLED"
    assert not launches


def test_completed_run_keeps_historical_evidence_when_current_coverage_rewinds(control):
    service, launches = control
    data = start(service, prepare(service))
    execute_worker(service.store, service.backend, *launches[0])
    service.backend.progress = Progress(observed_at=shown())
    result = service.call("get_collection_run", {"run_id": data["run_id"]})
    assert result["outcome"] == "partial"
    assert result["data"]["state"] == "completed"
    assert all(result["data"]["completion"].values())
    assert result["warnings"][0]["code"] == "CURRENT_COVERAGE_CHANGED"


def test_owner_is_persistent(tmp_path):
    path = tmp_path / "owner.toml"
    assert local_owner(path) == local_owner(path)


def test_detached_launcher_uses_exact_interpreter_and_no_shell(tmp_path, monkeypatch):
    import chainscope.agent_worker as worker

    seen = []
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    monkeypatch.setattr(
        worker.subprocess, "Popen", lambda args, **kwargs: seen.append((args, kwargs))
    )
    identity, ticket = str(uuid4()), str(uuid4())
    worker.launch(identity, ticket)
    argv, kwargs = seen[0]
    assert argv[0] == worker.sys.executable and argv[1] == "-B"
    assert argv[-4:] == ["--run", identity, "--ticket", ticket]
    assert not kwargs.get("shell")
    assert kwargs["stdin"] == worker.subprocess.DEVNULL


def test_uncertain_launch_keeps_same_run_and_can_be_fenced(control):
    service, _ = control

    def uncertain(*args):
        raise OSError("secret must not leak")

    service.launcher = uncertain
    plan = prepare(service)
    result = service.call("start_collection", {"plan_id": plan["plan_id"]})
    assert result["error"]["code"] == "LAUNCH_UNCONFIRMED"
    identity = result["error"]["related_run_id"]
    again = service.call("start_collection", {"plan_id": plan["plan_id"]})
    assert again["data"]["run"]["run_id"] == identity
    assert "secret" not in json.dumps(result)
    service.store.recover_dead(identity, OWNER, lambda *_: pytest.fail("unclaimed"))


def test_agents_sdk_round_trip_with_mock_deepseek(control):
    service, _ = control
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_collection_status",
                            "arguments": json.dumps(
                                {"token_id": "bem", "reference": "local"}
                            ),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            tool = next(m for m in body["messages"] if m["role"] == "tool")
            assert tool["tool_call_id"] == "call_1"
            assert json.loads(tool["content"])["data"]["token"]["token_id"] == "bem"
            message, finish = (
                {"role": "assistant", "content": "本地进度已读取。"},
                "stop",
            )
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    async def run():
        from chainscope.agent_tools import ToolContext

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with AsyncOpenAI(
                api_key="dummy", base_url="https://api.deepseek.com", http_client=http
            ) as client:
                result = await Runner.run(
                    build_agent(client, "test", read_only=True),
                    "进度？",
                    context=ToolContext(service, str(uuid4())),
                    run_config=RunConfig(tracing_disabled=True),
                )
                assert result.final_output == "本地进度已读取。"

    asyncio.run(run())
    assert len(requests) == 2
