"""Agent migrations and real InnoDB races in a disposable, randomly named database."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import sqlalchemy as sa
from conftest import requires_mysql
from test_agent_collection import FakeBackend, prepare
from test_mysql import MANIFEST, event, new_batch

from chainscope.agent_backend import Backend
from chainscope.agent_models import AGENT_TABLES, attempts, events, plans, runs
from chainscope.agent_service import Service
from chainscope.agent_store import Store
from chainscope.config import load_settings
from chainscope.database import check_database, initialize, store_batch
from chainscope.models import jobs, transfers
from chainscope.rpc_client import RpcClient
from chainscope.token_registry import AgentError, Registry

pytestmark = requires_mysql


def test_mysql_migration_preserves_old_events_and_core_0008_works(mysql_engine):
    engine = mysql_engine
    batch = new_batch(engine)
    store_batch(engine, batch, [event()], MANIFEST)
    with engine.begin() as c:
        before = dict(c.execute(sa.select(transfers)).mappings().one())
        # Reconstruct the old schema in this owned test database only.
        for table in (events, attempts, runs, plans):
            table.drop(c)
        c.execute(sa.text("UPDATE alembic_version SET version_num='0008'"))
    old = check_database(engine, full=True)
    assert old["schema_ready"] and not old["agent_schema_ready"]
    assert old["schema_differences"] == []
    from dataclasses import replace

    with engine.connect() as c:
        name = c.scalar(sa.text("SELECT DATABASE()"))
    assert name.startswith("chainscope_test_")
    assert initialize(replace(load_settings(), mysql_database=name))[
        "agent_schema_ready"
    ]
    assert check_database(engine, full=True)["schema_differences"] == []
    with engine.connect() as c:
        assert before == dict(c.execute(sa.select(transfers)).mappings().one())
        assert all(t in sa.inspect(c).get_table_names() for t in AGENT_TABLES)


@pytest.mark.parametrize("same_plan", [True, False])
def test_mysql_concurrent_reservation_is_single_run(mysql_engine, same_plan):
    owner = str(uuid4())
    store = Store(mysql_engine)
    service = Service(store, FakeBackend(), owner)
    a = prepare(service)
    b = a if same_plan else prepare(service)
    for p in (a, b):
        store.authorize(p["plan_id"], owner, p["plan_digest"])

    def reserve(p):
        try:
            data, ticket = store.reserve(p["plan_id"], owner)
            return data.run_id, ticket
        except AgentError as exc:
            assert exc.code == "JOB_BUSY"
            return None, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, (a, b)))
    assert sum(ticket is not None for _, ticket in results) == 1
    with mysql_engine.connect() as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(runs)) == 1


def test_backend_new_token_metadata_and_existing_task_snapshot(mysql_engine):
    registry = Registry.load()
    token = registry.tokens[0]
    responses = []

    def handler(request):
        import json

        import httpx

        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        responses.append((method, params))
        if method == "eth_chainId":
            result = "0x38"
        elif method == "eth_getBlockByNumber":
            result = {
                "number": "0x14",
                "hash": "0x" + "a" * 64,
                "timestamp": "0x65000000",
            }
        elif method == "eth_getCode":
            result = "0x1234" if int(params[1], 16) >= 5 else "0x"
        elif method == "eth_call":
            result = "0x8" if params[0]["data"] == "0x313ce567" else hex(2**200)
        else:
            raise AssertionError(method)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    import httpx

    from chainscope.agent_contracts import AtBlock
    from chainscope.jobs import create_job
    from chainscope.sqd import PARSER_VERSION

    backend = Backend(
        mysql_engine,
        load_settings(),
        lambda: RpcClient(
            "https://example.invalid",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ),
    )
    target, spec, progress = backend.target_and_job(
        token, AtBlock(mode="block", end_block=20), registry.policy
    )
    assert spec.create_required and spec.deploy_block == 5 and spec.decimals == 8
    with mysql_engine.connect() as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(jobs)) == 0
    create_job(
        mysql_engine,
        "original_job",
        token.address,
        source="sqd",
        start_block=5,
        deploy_block=5,
        window_size=50000,
        parser_version=PARSER_VERSION,
        decimals=8,
    )
    _, existing, progress = backend.target_and_job(
        token, AtBlock(mode="block", end_block=20), registry.policy
    )
    assert existing.job_id == "original_job" and not existing.create_required
    assert progress.transfer_events == 0 and progress.transfers_through is None
    assert target.block == 20


@pytest.mark.parametrize("timed", [False, True])
def test_agent_worker_bridges_real_collector_with_mock_sources(
    mysql_engine, monkeypatch, timed
):
    import json

    import httpx
    from test_hybrid import Paged
    from test_jobs import TOKEN, Chain, new_job

    import chainscope.agent_backend as backend_module
    from chainscope.agent_worker import execute_worker
    from chainscope.models import jobs, transfers

    registry = Registry.load()
    registry.tokens[0].address = TOKEN
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, *args: registry))
    new_job(mysql_engine)
    with mysql_engine.begin() as c:
        c.execute(jobs.update().values(deploy_block=10))
    chain = Chain(19 if timed else 14, {11: 3, 16: 2})

    class Source(Paged):
        def handler(self, request):
            body = json.loads(request.content)
            if body["method"] in ("eth_chainId", "eth_getCode"):
                result = "0x38" if body["method"] == "eth_chainId" else "0x1234"
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
                )
            return super().handler(request)

    source = Source(chain)
    backend = Backend(mysql_engine, load_settings(), source.client)
    monkeypatch.setattr(backend_module, "portal_for", lambda *_: chain.portal())
    store = Store(mysql_engine)
    owner = str(uuid4())
    launches = []
    service = Service(
        store,
        backend,
        owner,
        registry_loader=lambda: registry,
        launcher=lambda *args: launches.append(args),
        approve=lambda *_: True,
    )
    if timed:
        from test_agent_intent import context_for, invoke

        intent, context = context_for(
            service, "把bem继续采集到2023-11-15 06:13:34，并入库"
        )
        prepared = invoke(
            context,
            "prepare_collection",
            {"token_id": "bem", "target": {"mode": "latest_finalized"}},
        )["data"]
        assert prepared["target"]["block"] == 14
        assert prepared["target"]["requested_time"] == "2023-11-15T06:13:34+08:00"
        result = invoke(context, "start_collection", {"plan_id": prepared["plan_id"]})
    else:
        prepared = prepare(service)
        result = service.call("start_collection", {"plan_id": prepared["plan_id"]})
    assert result["outcome"] == "success", result
    assert execute_worker(store, backend, *launches[0])
    _, data = store.run(result["data"]["run"]["run_id"], owner)
    assert data.state == "completed", data
    assert data.completion.complete and data.target.block == 14
    assert data.budget.used_http_attempts > 0
    with mysql_engine.connect() as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(transfers)) == 3
        assert c.scalar(sa.select(sa.func.max(transfers.c.block_number))) == 11


def test_mysql_final_event_and_report_lease_are_serialized(mysql_engine):
    import os

    import psutil
    from test_agent_collection import start

    owner = str(uuid4())
    store = Store(mysql_engine)
    launches = []
    service = Service(
        store,
        FakeBackend(),
        owner,
        launcher=lambda *a: launches.append(a),
        approve=lambda *_: True,
    )
    run = start(service, prepare(service))
    run_id, ticket = launches[0]
    _, data, attempt_id = store.claim(
        run_id, ticket, os.getpid(), psutil.Process().create_time()
    )
    from chainscope.agent_store import shown

    data.state, data.ended_at = "paused", shown()

    def finish(_):
        return store.save_run(run_id, ticket, data, attempt_id=attempt_id, final=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        identities = list(pool.map(finish, range(2)))
    assert identities[0] == identities[1]
    assert store.journal.pending(owner) == [identities[0]]
    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(
            pool.map(lambda _: store.journal.claim(identities[0], owner), range(2))
        )
    assert sum(value is not None for value in leases) == 1
    assert store.journal.get(identities[0], owner).run.run_id == run["run_id"]


def test_mysql_history_date_scope_and_immutable_event(mysql_engine):
    from test_agent_collection import start

    from chainscope.agent_worker import execute_worker

    launches = []
    service = Service(
        Store(mysql_engine),
        FakeBackend(),
        str(uuid4()),
        launcher=lambda *a: launches.append(a),
        approve=lambda *_: True,
    )
    run = start(service, prepare(service))
    execute_worker(service.store, service.backend, *launches[0])
    event_id = service.store.journal.pending(service.owner_id)[0]
    before = service.call("get_collection_event", {"event_id": event_id})["data"]["run"]
    service.backend.progress.transfers_through = 22
    assert (
        service.call("get_collection_event", {"event_id": event_id})["data"]["run"]
        == before
    )
    history = service.call(
        "list_collection_runs",
        {
            "created_from": "2000-01-01T00:00:00+08:00",
            "created_to": "2100-01-01T00:00:00+08:00",
        },
    )
    assert history["outcome"] == "success", history
    assert history["data"]["items"][0]["run_id"] == run["run_id"]
    assert history["data"]["items"][0]["latest_event_id"] == event_id


@pytest.mark.skipif(
    __import__("os").environ.get("CHAINSCOPE_TEST_DEEPSEEK") != "1",
    reason="opt-in paid report compatibility check",
)
def test_live_deepseek_report_uses_disposable_snapshot(mysql_engine):
    import asyncio

    from openai import AsyncOpenAI
    from test_agent_collection import start

    from chainscope.agent_reports import deliver_report
    from chainscope.agent_runtime import provider_settings
    from chainscope.agent_worker import execute_worker

    launches = []
    service = Service(
        Store(mysql_engine),
        FakeBackend(),
        str(uuid4()),
        launcher=lambda *a: launches.append(a),
        approve=lambda *_: True,
    )
    start(service, prepare(service))
    execute_worker(service.store, service.backend, *launches[0])
    identity = service.store.journal.pending(service.owner_id)[0]
    displayed = []

    async def display(text):
        displayed.append(text)

    async def run():
        key, base, model = provider_settings()
        async with AsyncOpenAI(
            api_key=key, base_url=base, timeout=60, max_retries=1
        ) as client:
            await deliver_report(service, identity, client, model, display)

    asyncio.run(run())
    assert len(displayed) == 1 and "采集完成" in displayed[0]
    assert f"事件编号：{identity}" in displayed[0]
    assert (
        service.store.journal.get(identity, service.owner_id).report.state
        == "displayed"
    )
    print("DeepSeek 真实报告调用通过；临时数据库、模拟数据源，未启动正式采集。")


@pytest.mark.skipif(
    __import__("os").environ.get("CHAINSCOPE_TEST_DEEPSEEK") != "1",
    reason="opt-in paid command and completion-report compatibility check",
)
def test_live_deepseek_one_command_starts_and_reports_without_confirmation(
    mysql_engine,
):
    import asyncio
    import threading

    from agents import RunConfig
    from openai import AsyncOpenAI
    from test_agent_intent import TimedBackend

    from chainscope.agent_runtime import interactive, provider_settings
    from chainscope.agent_worker import execute_worker

    service = Service(Store(mysql_engine), TimedBackend(), str(uuid4()))
    threads, displayed = [], []

    def launch(*args):
        thread = threading.Thread(
            target=execute_worker, args=(service.store, service.backend, *args)
        )
        threads.append(thread)
        thread.start()

    service.launcher = launch

    class Console:
        def __init__(self):
            self.asked = False
            self.done = asyncio.Event()

        async def ask(self, prompt):
            if not self.asked:
                self.asked = True
                return "把bem从已有进度继续采集到2026-10-18 19:40，并入库"
            await asyncio.wait_for(self.done.wait(), timeout=120)
            return "/exit"

        def approve(self, *_):
            pytest.fail("explicit collection must not ask for another confirmation")

        async def display(self, text):
            displayed.append(text)
            if "事件编号：" in text and "采集完成" in text:
                self.done.set()

        def close(self):
            pass

    async def run():
        key, base, model = provider_settings()
        async with AsyncOpenAI(
            api_key=key, base_url=base, timeout=60, max_retries=1
        ) as client:
            await interactive(
                service,
                client,
                model,
                RunConfig(tracing_disabled=True, trace_include_sensitive_data=False),
                console=Console(),
            )

    try:
        asyncio.run(run())
    finally:
        for thread in threads:
            thread.join(timeout=10)
    assert len(threads) == 1
    assert any(
        "已接受启动请求" in text or "已找到对应采集运行" in text for text in displayed
    ), displayed
    assert any("事件编号：" in text and "采集完成" in text for text in displayed), (
        displayed
    )
    assert not any('"plan_id"' in text or "同意请输入" in text for text in displayed)
    recent = service.store.journal.recent_runs(service.owner_id)
    assert recent["bem"].state == "completed"
    assert recent["bem"].target.requested_time == "2026-10-18T19:40:00+08:00"
    print(
        "DeepSeek 单条指令启动、指定时间与自动结束报告通过；仅使用临时库和模拟数据源。"
    )
