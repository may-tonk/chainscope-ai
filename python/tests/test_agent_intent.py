"""Human-command authorization and exact cutoff boundaries, without formal writes."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from test_agent_collection import FakeBackend, prepare, start
from test_agent_notifications import service as service

from chainscope.agent_backend import block_at_or_before
from chainscope.agent_contracts import AtBlock, AtTime
from chainscope.agent_intent import UserIntent, preview_summary, start_summary
from chainscope.agent_models import events, plans, runs
from chainscope.agent_tools import ToolContext, make_tools
from chainscope.agent_worker import execute_worker
from chainscope.query import BEIJING
from chainscope.token_registry import AgentError, Registry

ISSUED = datetime(2026, 10, 19, 20, 0, tzinfo=BEIJING)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("采集 bem 到当前时间（19:40），并入库", "2026-10-19T19:40:00+08:00"),
        ("继续采集 bem 到19:40:12", "2026-10-19T19:40:12+08:00"),
        ("采集 bem 截至昨天19:40", "2026-10-18T19:40:00+08:00"),
        ("采集 bem 截至2026年10月18日19点40分", "2026-10-18T19:40:00+08:00"),
        ("采集 bem 到2026-10-18T11:40:00Z", "2026-10-18T19:40:00+08:00"),
        ("采集 bem 到2026-10-18 19:40+08:00", "2026-10-18T19:40:00+08:00"),
        ("采集 bem 到2026-10-18 11:40 UTC", "2026-10-18T19:40:00+08:00"),
        ("采集 bem 到2026-10-18 19:40 GMT+08:00", "2026-10-18T19:40:00+08:00"),
        ("采集 bem 到10/18 19:40", "2026-10-18T19:40:00+08:00"),
        ("采集 bem 到10-18 19:40", "2026-10-18T19:40:00+08:00"),
        ("采集 bem 到昨天11:40 UTC", "2026-10-18T19:40:00+08:00"),
        ("采集 bem 到昨天19:40 UTC+08:00", "2026-10-18T19:40:00+08:00"),
        ("采集 bem 到10月18日晚上7点40分", "2026-10-18T19:40:00+08:00"),
    ],
)
def test_deadline_preserves_user_clock_and_timezone(text, expected):
    intent = UserIntent.capture(text, Registry.load(), issued_at=ISSUED)
    assert intent.start and intent.clarification is None
    assert intent.target == {"mode": "time", "end_time": expected}


@pytest.mark.parametrize(
    "text",
    [
        "把 bem 从已有进度继续采集到当前时间，并进行入库",
        "请帮我采集bem到19:40，可以吗？",
        "如果没有问题就采集bem到19:40",
        "不要再让我确认，直接采集bem到19:40",
        "启动bem采集",
    ],
)
def test_clear_directives_are_authorized_without_second_confirmation(text):
    assert UserIntent.capture(text, Registry.load(), issued_at=ISSUED).start


@pytest.mark.parametrize(
    "text",
    [
        "现在是否可以采集bem到19:40？",
        "先展示bem采集到19:40的计划，不要执行",
        "不要采集bem",
        "查看bem采集进度",
        "解释一下‘采集bem’是什么意思",
        "如果采集bem会怎么样",
        "只看bem采集计划",
        "查询bem采集历史",
        "bem采集到19:40了吗",
        "bem采集完成了吗",
        "bem已经采集完成了",
        "我已经让LLM采集bem",
        "bem正在采集",
    ],
)
def test_questions_negation_and_preview_never_authorize_a_start(text):
    intent = UserIntent.capture(text, Registry.load(), issued_at=ISSUED)
    assert not intent.start and "start_collection" not in intent.tool_names


@pytest.mark.parametrize(
    "text",
    [
        "采集bem到明天19:40",
        "采集bem到23:00",
        "采集bem到上周五19:40",
        "采集bem到2026-02-30 19:40",
        "采集bem从10:00到19:40",
        "采集bem到昨天",
        "采集bem到7点40分",
        "采集bem到19:40:90",
        "采集bem到2026-10-18T19:40+08:00 UTC",
        "采集bem到19:40 UTC+08:80",
        "采集bem到10/18",
        "采集bem到10分钟前",
        "采集bem到19点半",
        "确认",
    ],
)
def test_ambiguous_or_future_cutoffs_are_not_silently_changed(text):
    intent = UserIntent.capture(text, Registry.load(), issued_at=ISSUED)
    assert not intent.start and intent.clarification


class TimedBackend(FakeBackend):
    def target_and_job(self, token, requested, policy):
        base = (
            AtBlock(mode="block", end_block=self.head)
            if requested.mode == "time"
            else requested
        )
        target, job, progress = super().target_and_job(token, base, policy)
        if requested.mode == "time":
            target.mode = "time"
            target.time_beijing = requested.end_time
            target.requested_time = requested.end_time
        return target, job, progress


def context_for(service, text, **kwargs):
    intent = UserIntent.capture(text, Registry.load(), issued_at=ISSUED, **kwargs)
    context = ToolContext(
        service, str(uuid4()), allowed_tools=intent.tool_names, authorization=intent
    )
    return intent, context


def invoke(context, name, arguments):
    tool = next(t for t in make_tools() if t.name == name)
    return json.loads(
        asyncio.run(
            tool.on_invoke_tool(SimpleNamespace(context=context), json.dumps(arguments))
        )
    )


def test_explicit_clock_overrides_model_latest_and_starts_without_console(service):
    service.backend = TimedBackend()
    service.approve = lambda *_: pytest.fail(
        "a clear instruction must not prompt again"
    )
    intent, context = context_for(
        service, "把bem从已有进度继续采集到当前时间（19:40），并入库"
    )
    result = invoke(
        context,
        "prepare_collection",
        {"token_id": "bem", "target": {"mode": "latest_finalized"}},
    )
    assert result["outcome"] == "success", result
    plan = result["data"]
    assert (
        plan["target"]["mode"] == "time"
        and plan["target"]["requested_time"] == "2026-10-19T19:40:00+08:00"
    )
    result = invoke(context, "start_collection", {"plan_id": plan["plan_id"]})
    assert result["outcome"] == "success" and len(service.launches) == 1
    text = start_summary(result["data"])
    assert "19:40:00" in text and plan["plan_id"] not in text and '"kind"' not in text
    assert intent.started is not None
    with service.store.engine.connect() as c:
        record = c.scalar(
            sa.select(events.c.payload).where(
                events.c.kind == "start_authorized_locally"
            )
        )
    assert record["authorization"]["source"] == "explicit_user_turn"
    assert record["authorization"]["target"]["mode"] == "time"


def test_preview_then_plain_confirmation_is_bound_to_same_plan(service):
    service.backend = TimedBackend()
    _, preview = context_for(service, "先展示bem采集到19:40的计划，不要执行")
    result = invoke(
        preview,
        "prepare_collection",
        {"token_id": "bem", "target": {"mode": "latest_finalized"}},
    )
    plan = preview.authorization.prepared
    text = preview_summary(plan)
    assert "确认" in text and plan.plan_id not in text and not service.launches
    assert (
        invoke(preview, "start_collection", {"plan_id": result["data"]["plan_id"]})[
            "outcome"
        ]
        == "error"
    )
    intent, confirmed = context_for(service, "确认", pending=plan)
    result = invoke(confirmed, "start_collection", {"plan_id": plan.plan_id})
    assert result["outcome"] == "success" and len(service.launches) == 1
    assert (
        intent.started["run"]["target"]["requested_time"] == plan.target.requested_time
    )


def test_model_only_preparing_still_fulfils_clear_request_once(service):
    service.backend = TimedBackend()
    intent, context = context_for(service, "采集bem到19:40并入库")
    result = intent.finish_requested_start(service, context.operation_id)
    assert result["outcome"] == "success" and len(service.launches) == 1
    assert intent.finish_requested_start(service, context.operation_id) is None


def test_readonly_turn_cannot_use_an_old_authorized_plan(service):
    plan = prepare(service)
    service.store.authorize(plan["plan_id"], service.owner_id, plan["plan_digest"])
    readonly, _ = context_for(service, "查看bem采集进度")
    result = service.call(
        "start_collection", {"plan_id": plan["plan_id"]}, authorization=readonly
    )
    assert result["error"]["code"] == "AUTHORIZATION_REQUIRED" and not service.launches


def test_model_cannot_change_token_or_increase_budget(service):
    intent, context = context_for(service, "采集bem到现在")
    reply = invoke(
        context,
        "prepare_collection",
        {"token_id": "other", "target": {"mode": "latest_finalized"}},
    )
    assert reply["error"]["code"] == "USER_SCOPE_MISMATCH"
    reply = invoke(
        context,
        "prepare_collection",
        {
            "token_id": "bem",
            "target": {"mode": "latest_finalized"},
            "max_runtime_seconds": 999999,
        },
    )
    assert reply["error"]["code"] == "INVALID_INPUT" and not service.launches


def test_resume_uses_original_target_and_does_not_reset_budget(service):
    service.backend.finish = "stop"
    run = start(service, prepare(service))
    execute_worker(service.store, service.backend, *service.launches[-1])
    _, previous = service.store.run(run["run_id"], service.owner_id)
    previous.budget.used_http_attempts = previous.budget.max_http_attempts
    with service.store.engine.begin() as c:
        c.execute(runs.update().values(payload=previous.model_dump(mode="json")))
    intent, context = context_for(
        service, "恢复bem原运行继续采集", known_runs={"bem": previous}
    )
    assert intent.target == {"mode": "resume", "run_id": previous.run_id}
    reply = intent.finish_requested_start(service, context.operation_id)
    assert reply is None and intent.prepared.plan_state == "blocked"
    assert any(i.code == "BUDGET_EXHAUSTED" for i in intent.prepared.blockers)
    assert len(service.launches) == 1


class Headers:
    def __init__(self, stamps):
        self.stamps, self.requested = stamps, []

    def call(self, method, params):
        number = int(params[0], 16)
        self.requested.append(number)
        return {
            "number": hex(number),
            "timestamp": hex(self.stamps[number]),
            "hash": "0x" + f"{number:064x}",
        }


def test_time_boundary_includes_every_block_of_a_shared_second():
    rpc = Headers([100, 101, 102, 102, 102, 103, 104])
    number, header = block_at_or_before(rpc, datetime.fromtimestamp(102, UTC), 6)
    assert number == 4 and header["timestamp"] == hex(102)
    assert 5 in rpc.requested and max(rpc.requested) == 6


def test_timestamp_between_blocks_and_unfinalized_tail():
    rpc = Headers([100, 102, 105])
    assert block_at_or_before(rpc, datetime.fromtimestamp(103, UTC), 2)[0] == 1
    with pytest.raises(AgentError, match="最终确认"):
        block_at_or_before(rpc, datetime.fromtimestamp(105, UTC), 2)
    with pytest.raises(AgentError, match="未来"):
        block_at_or_before(rpc, datetime.now(UTC) + timedelta(hours=1), 2)


def test_tool_time_contract_requires_timezone():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AtTime(mode="time", end_time="2026-10-19T19:40:00")


def test_old_signed_plan_remains_readable_without_rewriting(service):
    from chainscope.token_registry import digest

    payload = prepare(service)
    payload["target"].pop("requested_time")
    signature = digest(
        {
            k: v
            for k, v in payload.items()
            if k not in {"plan_digest", "authorization_state"}
        }
    )
    payload["plan_digest"] = signature
    with service.store.engine.begin() as c:
        c.execute(
            plans.update()
            .where(plans.c.plan_id == payload["plan_id"])
            .values(payload=payload, digest=signature)
        )
    _, plan = service.store.plan(payload["plan_id"], service.owner_id)
    assert plan.target.requested_time is None and plan.plan_digest == signature
    with service.store.engine.connect() as c:
        assert c.scalar(sa.select(plans.c.payload)) == payload
