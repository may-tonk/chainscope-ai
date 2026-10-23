"""Bounded acceptance detects real differences without mutating job data."""

from collections import Counter
from copy import deepcopy

import pytest
import sqlalchemy as sa
from conftest import requires_mysql

from chainscope.acceptance import (
    accept_sample,
    canonical_events,
    compare_events,
    coverage,
    missing_blocks,
)
from chainscope.database import utcnow
from chainscope.errors import ChainScopeError, SourceUnavailable
from chainscope.jobs import create_job
from chainscope.models import (
    balances,
    jobs,
    pool_scans,
    supplement_jobs,
    swap_windows,
    transfers,
    window_events,
    windows,
)
from chainscope.validator import TRANSFER_TOPIC

TOKEN = "0x" + "1" * 40
SENDER = "0x" + "2" * 40
RECEIVER = "0x" + "3" * 40
JOB = "acceptance_readonly_case"


def window(first=10, last=14, status="done", events=1):
    return dict(start_block=first, end_block=last, status=status, event_count=events)


def test_coverage_contiguity_is_independent_of_event_counts():
    assert coverage([window(events=0), window(15, 19)], 10, 19)["ok"]
    assert not coverage([], 10, 19)["ok"]
    assert not coverage([window(), window(16, 19)], 10, 19)["ok"]
    assert not coverage([window(), window(14, 19)], 10, 19)["ok"]
    assert not coverage([window(status="failed")], 10, 14)["ok"]
    assert not coverage([window(events=None)], 10, 14)["ok"]
    assert not coverage([window(last=20)], 10, 19)["ok"]


def test_empty_block_selection_covers_edges_and_all_empty():
    assert missing_blocks([11, 13], 10, 14) == [10, 12]
    assert missing_blocks([], 10, 14) == [10, 11]
    assert missing_blocks([10, 11, 12], 10, 12) == []


class ReadRpc:
    def __init__(self, mode="clean"):
        self.requests = 0
        self.calls = Counter()
        self.params = []
        self.mode = mode
        self.balances = {"0x" + format(i + 100, "040x"): 10**40 + i for i in range(12)}

    def header(self, number):
        return {
            "number": hex(number),
            "hash": "0x" + format(number, "064x"),
            "timestamp": hex(1_700_000_000 + number),
        }

    def logs(self, number):
        return [
            dict(
                blockNumber=hex(number),
                blockHash=self.header(number)["hash"],
                transactionHash="0x" + format(number * 100 + i, "064x"),
                logIndex=hex(i),
                address=TOKEN,
                removed=False,
                topics=[
                    TRANSFER_TOPIC,
                    "0x" + "0" * 24 + SENDER[2:],
                    "0x" + "0" * 24 + RECEIVER[2:],
                ],
                data="0x" + format(10**50 + i, "064x"),
            )
            for i in range({11: 1, 17: 2, 23: 3}.get(number, 0))
        ]

    def call(self, method, params):
        self.requests += 1
        self.calls[method] += 1
        self.params.append((method, params))
        if method == "eth_chainId":
            return "0x38"
        if method == "eth_getBlockByNumber":
            return self.header(int(params[0], 16))
        if method == "eth_getLogs":
            assert params[0]["fromBlock"] == params[0]["toBlock"]
            if self.mode == "source_error":
                raise SourceUnavailable("测试来源不可用")
            logs = self.logs(int(params[0]["fromBlock"], 16))
            return [] if self.mode == "log_missing" else logs
        if method == "eth_call":
            assert params[1] == hex(24)
            address = "0x" + params[0]["data"][-40:]
            return hex(self.balances[address] + (self.mode == "holder_bad"))
        raise AssertionError(f"Unexpected method: {method}")


@pytest.mark.parametrize("change", ["removed", "block", "padding", "duplicate"])
def test_malformed_logs_are_not_treated_as_empty(change):
    rpc = ReadRpc()
    logs = rpc.logs(11)
    if change == "removed":
        logs[0]["removed"] = True
    elif change == "block":
        logs[0]["blockNumber"] = hex(12)
    elif change == "padding":
        logs[0]["topics"][1] = "0x" + "f" * 64
    else:
        logs.append(deepcopy(logs[0]))
    with pytest.raises(ChainScopeError):
        canonical_events(logs, rpc.header(11), TOKEN)


def test_event_comparison_detects_same_count_different_content_and_missing():
    rpc = ReadRpc()
    rows = canonical_events(rpc.logs(17), rpc.header(17), TOKEN)
    assert compare_events(rows, rows)["ok"]
    changed = deepcopy(rows)
    changed[0]["amount_raw"] = str(int(changed[0]["amount_raw"]) + 1)
    assert compare_events(rows, changed)["changed"][0]["fields"] == ["amount_raw"]
    assert len(compare_events(rows[:1], rows)["missing_in_database"]) == 1
    assert len(compare_events(rows, rows[:1])["extra_in_database"]) == 1
    with pytest.raises(ChainScopeError):
        compare_events(rows + rows, rows)


def populate(engine, rpc):
    create_job(
        engine,
        JOB,
        TOKEN,
        source="alchemy",
        start_block=10,
        window_size=5,
        parser_version=1,
        decimals=18,
    )
    now = utcnow()
    with engine.begin() as c:
        c.execute(
            jobs.update()
            .where(jobs.c.job_id == JOB)
            .values(verified_through=24, balances_through=24)
        )
        c.execute(
            supplement_jobs.insert().values(
                job_id=JOB, verified_through=24, updated_at_utc=now
            )
        )
        c.execute(
            pool_scans.insert().values(
                job_id=JOB,
                configuration="0" * 64,
                scanned_through=24,
                updated_at_utc=now,
            )
        )
        for start, number in ((10, 11), (15, 17), (20, 23)):
            rows = canonical_events(rpc.logs(number), rpc.header(number), TOKEN)
            c.execute(
                windows.insert().values(
                    job_id=JOB,
                    **window(start, start + 4, events=len(rows)),
                    updated_at_utc=now,
                )
            )
            c.execute(
                swap_windows.insert().values(
                    job_id=JOB, **window(start, start + 4, events=0), updated_at_utc=now
                )
            )
            for row in rows:
                result = c.execute(
                    transfers.insert().values(
                        chain_id=56, token_address=TOKEN, token_decimals=18, **row
                    )
                )
                c.execute(
                    window_events.insert().values(
                        job_id=JOB,
                        start_block=start,
                        event_id=result.inserted_primary_key[0],
                    )
                )
        c.execute(
            balances.insert(),
            [
                dict(
                    chain_id=56,
                    token_address=TOKEN,
                    address=a,
                    balance_raw=str(v),
                    last_block=24,
                )
                for a, v in rpc.balances.items()
            ],
        )


@requires_mysql
@pytest.mark.parametrize("mode", ["clean", "log_missing", "holder_bad", "source_error"])
def test_live_mysql_snapshot_readonly_differences_and_bounded_calls(mysql_engine, mode):
    engine, rpc = mysql_engine, ReadRpc(mode)
    populate(engine, rpc)
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        report = accept_sample(engine, rpc, JOB, 24, seed=123)
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)
    assert all(
        s.lstrip().upper().startswith(("SELECT", "START TRANSACTION"))
        for s in statements
    )
    assert statements[0] == "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
    assert report["coverage"]["transfers"]["ok"]
    assert report["coverage"]["swaps"]["ok"]
    assert report["ok"] == (mode == "clean")
    assert report["full_history_source_verified"] is False
    assert len(report["blocks"]) <= 8
    assert len({r["address"] for r in report["holders"]}) == 10
    assert rpc.requests <= 27
    if mode == "clean":
        repeat = accept_sample(engine, ReadRpc(), JOB, 24, seed=123)
        assert repeat["holders"] == report["holders"]
        assert any(r["chain_events"] == 0 for r in report["blocks"])
        assert any(r["chain_events"] > 0 for r in report["blocks"])
        assert sum(r["selection"] == "top" for r in report["holders"]) == 5
    if mode == "source_error":
        assert report["blocks"][0]["status"] == "inconclusive"
        assert all(r["status"] == "not_checked" for r in report["holders"])
        assert rpc.calls["eth_getLogs"] == 1


@requires_mysql
def test_cutoff_mismatch_and_unfinished_coverage_prevent_external_work(mysql_engine):
    engine, rpc = mysql_engine, ReadRpc()
    populate(engine, rpc)
    with pytest.raises(ChainScopeError, match="截止区块"):
        accept_sample(engine, rpc, JOB, 23)
    assert rpc.requests == 0
    with engine.begin() as c:
        c.execute(
            swap_windows.update()
            .where(swap_windows.c.job_id == JOB, swap_windows.c.start_block == 15)
            .values(status="failed")
        )
    result = accept_sample(engine, rpc, JOB, 24)
    assert result["status"] == "coverage_failed"
    assert not result["ok"] and rpc.requests == 0


@requires_mysql
def test_server_rejects_writes_inside_acceptance(mysql_engine, monkeypatch):
    import chainscope.acceptance as acceptance

    engine, rpc = mysql_engine, ReadRpc()
    populate(engine, rpc)
    original = acceptance.select_blocks

    def attempt_write(c, job, done, cutoff):
        with pytest.raises(sa.exc.OperationalError) as failure:
            c.execute(jobs.update().where(jobs.c.job_id == JOB).values(status="paused"))
        assert failure.value.orig.args[0] == 1792  # MySQL read-only transaction
        return original(c, job, done, cutoff)

    monkeypatch.setattr(acceptance, "select_blocks", attempt_write)
    assert accept_sample(engine, rpc, JOB, 24)["ok"]
    with engine.connect() as c:
        assert (
            c.scalar(sa.select(jobs.c.status).where(jobs.c.job_id == JOB)) == "active"
        )
