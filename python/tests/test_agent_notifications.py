"""End-to-end report evidence, replay, notification and history boundaries."""

import asyncio
import copy
import io
import json
import os
import threading
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from agents import RunConfig
from openai import AsyncOpenAI
from test_agent_collection import FakeBackend, prepare, start

from chainscope.agent_console import AsyncConsole
from chainscope.agent_contracts import HistoryInput
from chainscope.agent_models import attempts, events, plans, runs
from chainscope.agent_notifications import LocalEvents, ProcessWatcher, wake
from chainscope.agent_reports import deliver_report, generate_report
from chainscope.agent_runtime import interactive
from chainscope.agent_service import Service
from chainscope.agent_store import Store, now
from chainscope.agent_tools import ToolContext, make_tools
from chainscope.agent_worker import execute_worker
from chainscope.token_registry import AgentError


@pytest.fixture
def service(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'events.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    for table in (plans, runs, attempts, events):
        table.create(engine)
    launches = []
    control = Service(
        Store(engine),
        FakeBackend(),
        str(uuid4()),
        launcher=lambda *args: launches.append(args),
        approve=lambda *_: True,
    )
    control.launches = launches
    yield control
    engine.dispose()


def finished(service):
    run = start(service, prepare(service))
    execute_worker(service.store, service.backend, *service.launches[-1])
    identity = service.store.journal.pending(service.owner_id)[-1]
    return run["run_id"], service.store.journal.get(identity, service.owner_id)


def test_final_snapshot_is_immutable_and_replayed_finish_is_one_event(service):
    identity, event = finished(service)
    before = event.run.model_dump()
    row, data = service.store.run(identity, service.owner_id)
    service.store.save_run(identity, row["ticket"], data, final=True)
    service.backend.progress.transfers_through = 30
    assert service.store.journal.pending(service.owner_id) == [event.event_id]
    assert (
        service.store.journal.get(event.event_id, service.owner_id).run.model_dump()
        == before
    )
    assert event.attempt_id is not None and event.run.completion.complete
    with pytest.raises(AgentError):
        service.store.journal.get(event.event_id, str(uuid4()))


def test_final_event_rolls_back_with_final_state(service, monkeypatch):
    run = start(service, prepare(service))
    run_id, ticket = service.launches[-1]
    row, data, attempt_id = service.store.claim(run_id, ticket, os.getpid(), 1.0)
    data.state, data.ended_at = "paused", "2026-10-19T20:00:00+08:00"

    def fail(*args, **kwargs):
        raise RuntimeError("injected commit boundary")

    monkeypatch.setattr(service.store.journal, "finish", fail)
    with pytest.raises(RuntimeError):
        service.store.save_run(run_id, ticket, data, attempt_id=attempt_id, final=True)
    assert service.store.run(run["run_id"], service.owner_id)[1].state == "starting"
    assert not service.store.journal.pending(service.owner_id)


def test_queued_stop_and_observer_make_one_event_without_attempt(service):
    run = start(service, prepare(service))
    service.store.stop(run["run_id"], service.owner_id)
    service.store.journal.observe_exit(run["run_id"], service.owner_id)
    pending = service.store.journal.pending(service.owner_id)
    assert len(pending) == 1
    event = service.store.journal.get(pending[0], service.owner_id)
    assert event.run.state == "stopped" and event.attempt_id is None
    assert not execute_worker(service.store, service.backend, *service.launches[0])


def test_pause_resume_records_distinct_episodes_of_same_run(service):
    service.backend.finish = "stop"
    run_id, pause = finished(service)
    assert pause.run.state == "paused"
    service.backend.finish = "complete"
    resumed = start(service, prepare(service, "resume", run_id=run_id))
    execute_worker(service.store, service.backend, *service.launches[-1])
    done = service.store.journal.get(
        service.store.journal.pending(service.owner_id)[-1], service.owner_id
    )
    assert resumed["run_id"] == done.run.run_id == run_id
    assert done.event_id != pause.event_id and done.attempt_id != pause.attempt_id
    assert done.run.state == "completed"
    assert (
        service.store.journal.get(pause.event_id, service.owner_id).run.state
        == "paused"
    )


def test_missing_worker_creates_uncertainty_without_restarting(service):
    run = start(service, prepare(service))
    with service.store.engine.begin() as c:
        row = c.scalar(sa.select(runs.c.payload))
        from chainscope.agent_store import shown

        row["queued_at"] = shown(now() - timedelta(minutes=2))
        c.execute(
            runs.update().values(accepted_at=now() - timedelta(minutes=2), payload=row)
        )
    for _ in range(2):
        service.store.journal.observe_exit(run["run_id"], service.owner_id)
    pending = service.store.journal.pending(service.owner_id)
    assert len(pending) == 1
    assert (
        service.store.journal.get(pending[0], service.owner_id).run.state == "unknown"
    )
    assert (
        service.store.run(run["run_id"], service.owner_id)[0]["active_job"] is not None
    )
    assert len(service.launches) == 1


def test_history_uses_creation_time_keyset_and_event_links(service):
    first, event = finished(service)
    service.backend.head = 21
    second = start(service, prepare(service))["run_id"]
    with service.store.engine.begin() as c:
        c.execute(runs.update().values(accepted_at=now()))
    result = service.call("list_collection_runs", {"limit": 1})
    top = result["data"]
    rest = service.call(
        "list_collection_runs", {"limit": 1, "before_run_id": top["next_cursor"]}
    )["data"]
    assert {top["items"][0]["run_id"], rest["items"][0]["run_id"]} == {first, second}
    assert rest["next_cursor"] is None
    combined = top["items"] + rest["items"]
    assert (
        next(i for i in combined if i["run_id"] == first)["latest_event_id"]
        == event.event_id
    )
    assert (
        service.call("list_collection_runs", {"created_from": "2026-10-19T00:00:00"})[
            "error"
        ]["code"]
        == "INVALID_INPUT"
    )
    assert service.call("list_collection_runs", {"limit": 51})["outcome"] == "error"
    assert service.store.journal.history(str(uuid4()), HistoryInput()).items == []
    with service.store.engine.connect() as c:
        stamp = c.scalar(sa.select(runs.c.accepted_at).where(runs.c.run_id == first))
    from datetime import UTC

    lower = stamp.replace(tzinfo=UTC).isoformat()
    assert (
        len(
            service.call("list_collection_runs", {"created_from": lower})["data"][
                "items"
            ]
        )
        == 2
    )
    assert (
        service.call("list_collection_runs", {"created_to": lower})["data"]["items"]
        == []
    )


def test_report_context_enforces_event_and_tool_scope(service):
    _, event = finished(service)
    context = SimpleNamespace(
        context=ToolContext(
            service,
            str(uuid4()),
            allowed_tools=frozenset({"get_collection_event"}),
            report_event_id=event.event_id,
        )
    )

    async def verify():
        for tool in make_tools():
            argument = (
                {"event_id": event.event_id + 1}
                if tool.name == "get_collection_event"
                else {}
            )
            reply = json.loads(await tool.on_invoke_tool(context, json.dumps(argument)))
            assert reply["outcome"] == "error"
        tool = next(t for t in make_tools() if t.name == "get_collection_event")
        assert (
            json.loads(await tool.on_invoke_tool(context, "[]"))["error"]["code"]
            == "INVALID_INPUT"
        )

    asyncio.run(verify())


@pytest.mark.parametrize("wrong_state", [False, True])
def test_model_report_queries_bound_event_and_checks_state(service, wrong_state):
    _, event = finished(service)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert [t["function"]["name"] for t in body["tools"]] == [
            "get_collection_event"
        ]
        if len(requests) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "bound_event",
                        "type": "function",
                        "function": {
                            "name": "get_collection_event",
                            "arguments": json.dumps({"event_id": event.event_id}),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            result = json.loads(
                next(m for m in body["messages"] if m["role"] == "tool")["content"]
            )
            assert result["data"]["run"]["state"] == "completed"
            draft = {
                "event_id": event.event_id,
                "run_id": event.run.run_id,
                "state": "paused" if wrong_state else "completed",
                "summary": "本次采集已到达固定目标，数据已经入库。",
                "explanation": "可以继续查询这次运行。",
                "next_steps": [],
            }
            message, finish = (
                {"role": "assistant", "content": json.dumps(draft, ensure_ascii=False)},
                "stop",
            )
        return httpx.Response(
            200,
            json={
                "id": "mock",
                "object": "chat.completion",
                "created": 1,
                "model": "mock",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with AsyncOpenAI(
                api_key="dummy", base_url="https://api.deepseek.com", http_client=http
            ) as client:
                return await generate_report(service, event, client, "mock")

    if wrong_state:
        with pytest.raises(AgentError, match="状态"):
            asyncio.run(run())
    else:
        text = asyncio.run(run())
        assert "本运行累计新增：转账 12 条；Swap 未统计。" in text
        assert f"事件编号：{event.event_id}" in text and "未统计" in text
    assert len(requests) == 2


def test_delivery_leases_saved_text_and_restart_replay(service):
    _, event = finished(service)
    calls, shown_text = [], []

    async def generator(*args):
        calls.append(1)
        return "已完成，保存这份报告。"

    async def display(text):
        shown_text.append(text)

    async def run():
        assert await deliver_report(
            service, event.event_id, None, None, display, generator=generator
        )
        assert (
            await deliver_report(
                service, event.event_id, None, None, display, generator=generator
            )
            is None
        )

    asyncio.run(run())
    assert len(calls) == len(shown_text) == 1
    assert not service.store.journal.pending(service.owner_id)
    assert service.store.journal.get(
        event.event_id, service.owner_id
    ).report.displayed_at


def test_failed_generation_is_bounded_and_retained_for_reopen(service):
    _, event = finished(service)
    calls = []

    async def unavailable(*args):
        calls.append(1)
        raise RuntimeError("api-key must not leak")

    async def display(text):
        pytest.fail("a failed generation cannot be acknowledged")

    with pytest.raises(AgentError) as error:
        asyncio.run(
            deliver_report(
                service, event.event_id, None, None, display, generator=unavailable
            )
        )
    assert "api-key" not in str(error.value) and len(calls) == 2
    assert (
        service.store.journal.get(event.event_id, service.owner_id).report.state
        == "failed"
    )
    assert service.store.journal.claim(event.event_id, service.owner_id) is not None


def test_live_lease_is_not_reclaimed_and_dead_lease_is_reclaimed(service):
    _, event = finished(service)
    _, lease = service.store.journal.claim(event.event_id, service.owner_id)
    assert service.store.journal.claim(event.event_id, service.owner_id) is None
    with service.store.engine.begin() as c:
        payload = copy.deepcopy(
            c.scalar(
                sa.select(events.c.payload).where(events.c.event_id == event.event_id)
            )
        )
        payload["report"]["lease_created"] -= 100
        c.execute(
            events.update()
            .where(events.c.event_id == event.event_id)
            .values(payload=payload)
        )
    assert service.store.journal.claim(event.event_id, service.owner_id)[1] != lease


def test_local_notification_roundtrip():
    owner, identity = str(uuid4()), str(uuid4())
    arrived, seen = threading.Event(), []

    def receive(value):
        seen.append(value)
        arrived.set()

    listener = LocalEvents(owner, receive)
    try:
        wake(owner, identity)
        assert arrived.wait(3) and seen == [identity]
    finally:
        listener.close()


def test_process_watcher_detects_exit_without_database_polling():
    import subprocess
    import sys

    import psutil

    arrived = threading.Event()
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", "import time; time.sleep(0.2)"],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    created = psutil.Process(process.pid).create_time()
    watcher = ProcessWatcher(lambda _: arrived.set())
    try:
        watcher.track(
            {"pid": process.pid, "process_started": created, "run_id": str(uuid4())}
        )
        process.wait(timeout=5)
        assert arrived.wait(3)
    finally:
        watcher.close()


def test_pipe_console_does_not_block_reports_or_consume_approval_input():
    class Input:
        def __init__(self):
            import queue

            self.lines = queue.Queue()

        def isatty(self):
            return False

        def readline(self):
            return self.lines.get()

    async def run():
        stream, output = Input(), io.StringIO()
        console = AsyncConsole(stream=stream, output=output)
        try:
            line = asyncio.create_task(console.ask("你 > "))
            await asyncio.sleep(0)
            await console.display("自动报告已经到达。")
            assert not line.done() and "自动报告" in output.getvalue()
            stream.lines.put("保留我正在输入的内容\n")
            assert await asyncio.wait_for(line, 2) == "保留我正在输入的内容"
        finally:
            console.close()

    asyncio.run(run())


def test_exit_before_claim_is_reported_and_old_exit_ticket_is_ignored(service):
    run = start(service, prepare(service))
    identity, ticket = service.launches[0]
    service.store.journal.observe_exit(
        identity, service.owner_id, expected_ticket=str(uuid4()), exited=True
    )
    assert not service.store.journal.pending(service.owner_id)
    service.store.journal.observe_exit(
        identity, service.owner_id, expected_ticket=ticket, exited=True
    )
    event = service.store.journal.get(
        service.store.journal.pending(service.owner_id)[0], service.owner_id
    )
    assert event.run.run_id == run["run_id"] and event.run.state == "unknown"
    assert event.attempt_id is None


def test_resumed_run_has_fresh_startup_reference(service):
    service.backend.finish = "stop"
    identity, _ = finished(service)
    with service.store.engine.begin() as c:
        c.execute(runs.update().values(accepted_at=now() - timedelta(days=1)))
    resumed = start(service, prepare(service, "resume", run_id=identity))
    assert service.get_run_data(identity).state == "queued"
    assert resumed["queued_at"] is not None
    service.store.journal.observe_exit(identity, service.owner_id)
    assert len(service.store.journal.pending(service.owner_id)) == 1


def test_child_handle_wakes_even_without_ipc():
    import subprocess
    import sys

    seen, arrived = [], threading.Event()

    def receive(value):
        seen.append(value)
        arrived.set()

    identity, ticket = str(uuid4()), str(uuid4())
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", "pass"],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    watcher = ProcessWatcher(receive)
    try:
        watcher.track_child(child, identity, ticket)
        assert arrived.wait(5) and seen == [(identity, ticket, True)]
    finally:
        watcher.close()


def test_startup_crash_pushes_uncertainty_before_worker_claim(service, monkeypatch):
    import subprocess
    import sys

    import chainscope.agent_runtime as runtime

    messages = []
    service.launcher = lambda *_: subprocess.Popen(
        [sys.executable, "-B", "-c", "pass"],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    async def model_turn(*args, **kwargs):
        context = kwargs["context"]
        prepared = await asyncio.to_thread(
            service.call,
            "prepare_collection",
            {"token_id": "bem", "target": {"mode": "latest_finalized"}},
            context.operation_id,
        )
        context.authorization.observe(prepared)
        result = await asyncio.to_thread(
            service.call,
            "start_collection",
            {"plan_id": prepared["data"]["plan_id"]},
            context.operation_id,
            authorization=context.authorization,
        )
        context.authorization.observe(result)
        return SimpleNamespace(
            final_output="启动请求已接受。", to_input_list=lambda: []
        )

    monkeypatch.setattr(runtime.Runner, "run", model_turn)

    async def run():
        delivered = asyncio.Event()

        class Console:
            def __init__(self):
                self.first = True

            def approve(self, *_):
                return True

            async def ask(self, _):
                if self.first:
                    self.first = False
                    return "启动 bem 采集"
                await asyncio.wait_for(delivered.wait(), 5)
                return "/exit"

            async def display(self, text):
                messages.append(text)
                if text == "启动期异常已报告。":
                    delivered.set()

            def close(self):
                pass

        async def generator(_, event, *args):
            assert event.run.state == "unknown" and event.attempt_id is None
            return "启动期异常已报告。"

        async def reporter(s, identity, client, model, display):
            return await deliver_report(
                s, identity, client, model, display, generator=generator
            )

        await interactive(
            service,
            None,
            None,
            RunConfig(tracing_disabled=True),
            console=Console(),
            reporter=reporter,
        )

    asyncio.run(run())
    assert messages[0].startswith("已接受启动请求")
    assert messages[1:] == ["启动期异常已报告。"]


def test_reopen_dispatches_pending_without_user_message(service):
    _, event = finished(service)
    reports = []

    class Console:
        approve = None

        async def ask(self, prompt):
            for _ in range(100):
                if reports:
                    return "/exit"
                await asyncio.sleep(0.01)
            raise AssertionError("no automatic report")

        async def display(self, text):
            reports.append(text)

        def close(self):
            pass

    async def generator(*args):
        return "重开对话后补报成功。"

    async def reporter(service, identity, client, model, display):
        return await deliver_report(
            service, identity, client, model, display, generator=generator
        )

    asyncio.run(
        interactive(
            service,
            None,
            None,
            RunConfig(tracing_disabled=True),
            console=Console(),
            reporter=reporter,
        )
    )
    assert reports == ["重开对话后补报成功。"]
    assert (
        service.store.journal.get(event.event_id, service.owner_id).report.state
        == "displayed"
    )


def test_worker_finish_pushes_report_while_console_waits_for_input(service):
    start(service, prepare(service))
    reports = []

    async def run():
        ready, done = asyncio.Event(), asyncio.Event()

        class Console:
            approve = None

            async def ask(self, prompt):
                ready.set()
                await asyncio.wait_for(done.wait(), 5)
                return "/exit"

            async def display(self, text):
                reports.append(text)
                done.set()

            def close(self):
                pass

        async def generator(*args):
            return "采集结束主动推送。"

        async def reporter(s, event_id, client, model, display):
            return await deliver_report(
                s, event_id, client, model, display, generator=generator
            )

        task = asyncio.create_task(
            interactive(
                service,
                None,
                None,
                RunConfig(tracing_disabled=True),
                console=Console(),
                reporter=reporter,
            )
        )
        await ready.wait()
        await asyncio.to_thread(
            execute_worker, service.store, service.backend, *service.launches[0]
        )
        await task

    asyncio.run(run())
    assert reports == ["采集结束主动推送。"]


def test_generated_but_unshown_report_reuses_text_without_model(service):
    _, event = finished(service)
    _, lease = service.store.journal.claim(event.event_id, service.owner_id)
    service.store.journal.update_report(
        event.event_id,
        service.owner_id,
        lease,
        state="failed",
        text="已生成但未展示的正文。",
    )
    shown_text = []

    async def generator(*args):
        pytest.fail("must not call model again for stored text")

    async def display(text):
        shown_text.append(text)

    asyncio.run(
        deliver_report(
            service, event.event_id, None, None, display, generator=generator
        )
    )
    assert shown_text == ["已生成但未展示的正文。"]


def test_tty_notification_preserves_partially_typed_input(monkeypatch):
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    import chainscope.agent_console as module

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    async def run():
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            monkeypatch.setattr(
                module,
                "PromptSession",
                lambda: PromptSession(input=pipe, output=DummyOutput()),
            )
            console = AsyncConsole(stream=Terminal(), output=Terminal())
            try:
                task = asyncio.create_task(console.ask("你 > "))
                await asyncio.sleep(0.01)
                pipe.send_text("我正在输入")
                await asyncio.sleep(0.01)
                await console.display("任务已经结束。")
                pipe.send_text("的内容\n")
                assert await asyncio.wait_for(task, 2) == "我正在输入的内容"
            finally:
                console.close()

    asyncio.run(run())
