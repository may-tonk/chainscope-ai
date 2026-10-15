"""Scan jobs: window planning, resume after failures and abandoned runs."""

import json

import httpx
import pytest
import sqlalchemy as sa
from conftest import requires_mysql
from sqlalchemy.exc import OperationalError

import chainscope.jobs as jobs_module
from chainscope.errors import ChainScopeError
from chainscope.jobs import (
    contiguous_through,
    create_job,
    job_lock,
    job_status,
    plan_ranges,
    split_range,
)
from chainscope.models import (
    batches,
    jobs,
    transfers,
    window_diffs,
    window_events,
    windows,
)
from chainscope.sqd import Portal
from chainscope.sync import sync_job
from chainscope.validator import TRANSFER_TOPIC

TOKEN = "0x" + "1" * 40
FROM = "0x" + "2" * 40
TO = "0x" + "3" * 40


def test_plan_ranges_cover_every_block_once():
    assert plan_ranges(10, 24, 5) == [(10, 14), (15, 19), (20, 24)]
    assert plan_ranges(10, 21, 5) == [(10, 14), (15, 19), (20, 21)]
    assert plan_ranges(25, 24, 5) == []
    with pytest.raises(ChainScopeError):
        plan_ranges(0, 10, 0)


def test_split_range_halves_without_overlap():
    assert split_range(10, 19) == ((10, 14), (15, 19))
    assert split_range(10, 11) == ((10, 10), (11, 11))
    with pytest.raises(ChainScopeError):
        split_range(10, 10)


def test_watermark_stops_at_the_first_gap():
    rows = [(10, 14, "done"), (15, 19, "failed"), (20, 24, "done")]
    assert contiguous_through(10, rows) == 14
    assert contiguous_through(10, [(10, 14, "pending")]) is None
    assert contiguous_through(10, [(11, 14, "done")]) is None  # hole at block 10
    assert contiguous_through(10, [(10, 14, "done"), (15, 19, "done")]) == 19


def topic(address):
    return "0x" + "0" * 24 + address[2:]


def chain_block(number, count):
    """A block with `count` Transfer logs; content depends only on the number."""
    return {
        "header": {
            "number": number,
            "hash": "0x" + format(number, "064x"),
            "timestamp": 1_700_000_000 + number,
        },
        "logs": [
            {
                "logIndex": i,
                "transactionHash": "0x" + format(number * 100 + i, "064x"),
                "address": TOKEN,
                "topics": [TRANSFER_TOPIC, topic(FROM), topic(TO)],
                "data": "0x" + format(number * 10 + i, "064x"),
            }
            for i in range(count)
        ],
    }


class Chain:
    """Serves Portal requests from a fixed set of blocks; can fail on demand."""

    def __init__(self, head, events_by_block):
        self.head = head
        self.events = events_by_block
        self.fail_from = None  # a block number whose request returns HTTP 400
        self.requests = []

    def handler(self, request):
        if request.url.path.endswith("/finalized-head"):
            return httpx.Response(200, json={"number": self.head})
        body = json.loads(request.content)
        self.requests.append((body["fromBlock"], body["toBlock"]))
        if (
            self.fail_from is not None
            and body["fromBlock"] <= self.fail_from <= body["toBlock"]
        ):
            return httpx.Response(400)
        blocks = [
            chain_block(n, self.events[n])
            for n in sorted(self.events)
            if body["fromBlock"] <= n <= body["toBlock"]
        ]
        content = b"".join(json.dumps(b).encode() + b"\n" for b in blocks)
        return httpx.Response(200, content=content)

    def portal(self):
        return Portal(
            "https://portal.invalid",
            "binance-mainnet",
            httpx.Client(transport=httpx.MockTransport(self.handler)),
            sleep=lambda _: None,
            min_interval=0,
        )


def new_job(engine, start=10, size=5):
    create_job(
        engine,
        "bem",
        TOKEN,
        source="sqd",
        start_block=start,
        window_size=size,
        parser_version=1,
        decimals=18,
    )


def run(engine, chain, **options):
    return sync_job(
        engine,
        "bem",
        chain.portal(),
        sleep=lambda _: None,
        notify=lambda _: None,
        **options,
    )


def count(engine, table):
    with engine.connect() as connection:
        return connection.scalar(sa.select(sa.func.count()).select_from(table))


def window_rows(engine):
    with engine.connect() as connection:
        return connection.execute(
            sa.select(
                windows.c.start_block, windows.c.end_block, windows.c.status
            ).order_by(windows.c.start_block)
        ).all()


@requires_mysql
def test_sync_collects_resumes_and_follows_new_blocks(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = Chain(24, {11: 2, 17: 1, 24: 3})
    first = run(engine, chain)
    assert first["stopped"] == "caught_up"
    assert first["windows_done"] == 3 and first["events"] == 6
    assert window_rows(engine) == [(10, 14, "done"), (15, 19, "done"), (20, 24, "done")]
    assert job_status(engine, "bem")["verified_through"] == 24
    # Running again with no new blocks is a no-op and duplicates nothing.
    second = run(engine, chain)
    assert second["windows_done"] == 0 and second["planned_windows"] == 0
    assert count(engine, transfers) == 6 and count(engine, window_events) == 6
    # New finalized blocks continue from where the last run ended.
    chain.head = 30
    chain.events[26] = 1
    third = run(engine, chain)
    assert third["windows_done"] == 2 and third["inserted"] == 1
    assert job_status(engine, "bem")["verified_through"] == 30
    assert job_status(engine, "bem")["events_in_done_windows"] == 7


@requires_mysql
def test_failed_window_blocks_watermark_until_retried(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = Chain(24, {11: 1, 17: 1, 24: 1})
    chain.fail_from = 17
    with pytest.raises(ChainScopeError, match="HTTP 400"):
        run(engine, chain)
    assert window_rows(engine)[:2] == [(10, 14, "done"), (15, 19, "failed")]
    status = job_status(engine, "bem")
    assert status["verified_through"] == 14
    assert status["failed_windows"][0]["error_type"] == "ChainScopeError"
    chain.fail_from = None
    run(engine, chain)
    assert job_status(engine, "bem")["verified_through"] == 24
    assert count(engine, transfers) == 3
    with engine.connect() as connection:
        statuses = connection.execute(
            sa.select(batches.c.status, sa.func.count()).group_by(batches.c.status)
        ).all()
    assert dict(statuses) == {"failed": 1, "succeeded": 3}


@requires_mysql
def test_abandoned_running_window_is_recovered(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = Chain(14, {12: 1})
    run(engine, chain, max_windows=0)
    with engine.begin() as connection:
        connection.execute(windows.update().values(status="running"))
    summary = run(engine, chain)
    assert summary["recovered_abandoned"] == 1
    assert window_rows(engine) == [(10, 14, "done")]


@requires_mysql
def test_dense_window_is_split_until_it_fits(mysql_engine):
    engine = mysql_engine
    new_job(engine, size=10)
    chain = Chain(19, {11: 1, 13: 1, 18: 1})
    summary = run(engine, chain, event_limit=1)
    assert summary["splits"] >= 2
    rows = window_rows(engine)
    assert all(status == "done" for _, _, status in rows)
    assert rows[0][0] == 10 and rows[-1][1] == 19
    assert all(a[1] + 1 == b[0] for a, b in zip(rows, rows[1:]))
    assert count(engine, transfers) == 3


@requires_mysql
def test_budget_stops_between_windows(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    summary = run(engine, Chain(24, {}), max_windows=1)
    assert summary["stopped"] == "max_windows"
    assert job_status(engine, "bem")["windows"] == {
        "done": 1,
        "pending": 2,
        "running": 0,
        "failed": 0,
    }


@requires_mysql
def test_second_runner_is_refused(mysql_engine):
    new_job(mysql_engine)
    with job_lock(mysql_engine, "bem"):
        with pytest.raises(ChainScopeError, match="已有进程"):
            run(mysql_engine, Chain(14, {}))


@requires_mysql
def test_conflicting_event_rolls_back_the_whole_window(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = Chain(14, {11: 1, 12: 1})
    run(engine, chain)
    with engine.begin() as connection:
        connection.execute(
            transfers.update()
            .where(transfers.c.block_number == 12)
            .values(amount_raw="1")
        )
        connection.execute(window_events.delete())
        connection.execute(windows.update().values(status="pending"))
        connection.execute(jobs.update().values(verified_through=None))
    # A conflict with stored data is quarantined with evidence, not retried.
    summary = run(engine, chain)
    assert [q["error_type"] for q in summary["quarantined"]] == ["EventConflict"]
    assert count(engine, window_events) == 0
    assert window_rows(engine) == [(10, 14, "failed")]
    with engine.connect() as connection:
        kinds = connection.scalars(sa.select(window_diffs.c.kind)).all()
    assert kinds == ["field_conflict"]
    assert run(engine, chain)["windows_done"] == 0  # stays quarantined
    assert job_status(engine, "bem")["failed_windows"][0]["quarantined"]


@requires_mysql
def test_lost_commit_acknowledgement_is_resolved_from_the_database(
    mysql_engine, monkeypatch
):
    engine = mysql_engine
    new_job(engine)
    real_store = jobs_module._store

    def store_then_lose_reply(*args):
        real_store(*args)
        raise OperationalError("COMMIT", {}, Exception(2013, "Lost connection"))

    monkeypatch.setattr(jobs_module, "_store", store_then_lose_reply)
    summary = run(engine, Chain(14, {11: 1}))
    assert summary["windows_done"] == 1
    assert job_status(engine, "bem")["verified_through"] == 14
    assert count(engine, transfers) == 1


@requires_mysql
def test_duplicate_job_is_refused(mysql_engine):
    new_job(mysql_engine)
    with pytest.raises(ChainScopeError, match="已有任务"):
        create_job(
            mysql_engine,
            "bem2",
            TOKEN,
            source="sqd",
            start_block=0,
            window_size=5,
            parser_version=1,
            decimals=None,
        )
