"""Cross-source reconciliation, recheck/redo, date queries and operations."""

import json

import httpx
import pytest
import sqlalchemy as sa
from conftest import requires_mysql
from test_jobs import FROM, TO, TOKEN, Chain, chain_block, count, new_job, window_rows

import chainscope.jobs as jobs_module
from chainscope.database import check_database
from chainscope.errors import ChainScopeError
from chainscope.jobs import job_status, verify_job
from chainscope.models import transfers, window_diffs, windows
from chainscope.query import BEIJING, coverage, parse_time, query_job, report
from chainscope.reconcile import compare_sources, sample_receipts
from chainscope.rpc_client import RpcClient, retry_wait
from chainscope.sync import recheck_job, redo_job, sync_job
from chainscope.validator import TRANSFER_TOPIC


def portal_events(chain, number):
    from chainscope.sqd import block_transfers

    return block_transfers(chain_block(number, chain.events[number]), TOKEN)


class Alchemy:
    """JSON-RPC stub that answers from the same synthetic chain as the Portal."""

    def __init__(self, chain, finalized=None):
        self.chain = chain
        self.finalized = finalized
        self.drop = set()  # (block, log_index) Alchemy "misses"
        self.methods = []

    def transfers(self, start, end):
        out = []
        for number in sorted(self.chain.events):
            if not start <= number <= end:
                continue
            for e in portal_events(self.chain, number):
                if (number, e["log_index"]) in self.drop:
                    continue
                out.append(
                    {
                        "category": "erc20",
                        "blockNum": hex(number),
                        "hash": e["tx_hash"],
                        "uniqueId": f"{e['tx_hash']}:log:{e['log_index']}",
                        "from": e["from_address"],
                        "to": e["to_address"],
                        "rawContract": {"value": hex(e["amount"]), "address": TOKEN},
                    }
                )
        return out

    def receipt(self, tx_hash):
        for number in self.chain.events:
            block = chain_block(number, self.chain.events[number])
            logs = [
                {
                    **log,
                    "logIndex": hex(log["logIndex"]),
                    "blockHash": block["header"]["hash"],
                    "blockNumber": hex(number),
                    "removed": False,
                }
                for log in block["logs"]
                if log["transactionHash"] == tx_hash
            ]
            if logs:
                return {
                    "status": "0x1",
                    "transactionHash": tx_hash,
                    "blockHash": block["header"]["hash"],
                    "blockNumber": hex(number),
                    "logs": logs,
                }
        return None

    def handler(self, request):
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.methods.append(method)
        if method == "alchemy_getAssetTransfers":
            p = params[0]
            result = {
                "transfers": self.transfers(
                    int(p["fromBlock"], 16), int(p["toBlock"], 16)
                )
            }
        elif method == "eth_getBlockByNumber":
            result = None if self.finalized is None else {"number": hex(self.finalized)}
        elif method == "eth_getTransactionReceipt":
            result = self.receipt(params[0])
        else:
            raise AssertionError(method)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    def client(self):
        return RpcClient(
            "https://example.invalid/key",
            client=httpx.Client(transport=httpx.MockTransport(self.handler)),
            sleep=lambda _: None,
        )


def run(engine, chain, rpc=None, **options):
    return sync_job(
        engine,
        "bem",
        chain.portal(),
        rpc=rpc,
        sleep=lambda _: None,
        notify=lambda _: None,
        **options,
    )


def test_retry_after_and_size_limit():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "9"})
        if len(calls) == 2:
            return httpx.Response(
                200, headers={"Content-Length": str(17 * 2**20)}, content=b"{}"
            )
        raise AssertionError

    waits = []
    rpc = RpcClient(
        "https://example.invalid/key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=waits.append,
    )
    with pytest.raises(ChainScopeError, match="16 MiB"):
        rpc.call("eth_chainId", [])
    assert waits == [9] and rpc.rate_limited == 1
    assert retry_wait(httpx.Response(503), 2) == 4
    assert retry_wait(httpx.Response(429, headers={"Retry-After": "999"}), 0) == 60


def test_compare_sources_reports_each_kind_of_difference():
    chain = Chain(0, {11: 3})
    ours = portal_events(chain, 11)
    theirs = {(e["tx_hash"], e["log_index"]): dict(e) for e in ours}
    first, second, third = ours
    del theirs[(first["tx_hash"], first["log_index"])]
    theirs[(second["tx_hash"], second["log_index"])]["amount"] += 1
    extra = dict(third, log_index=99)
    theirs[(extra["tx_hash"], 99)] = extra
    kinds = sorted(d["kind"] for d in compare_sources(ours, theirs))
    assert kinds == ["field_conflict", "missing_in_alchemy", "missing_in_sqd"]
    assert (
        compare_sources(ours, {(e["tx_hash"], e["log_index"]): e for e in ours}) == []
    )


def test_receipt_sample_is_seeded_and_checks_every_event_of_the_tx():
    chain = Chain(0, {n: 1 for n in range(10, 20)})
    alchemy = Alchemy(chain)
    rows = []
    for n in range(10, 20):
        for e in portal_events(chain, n):
            row = dict(e, amount_raw=str(e.pop("amount")))
            rows.append(row)
    with alchemy.client() as rpc:
        assert sample_receipts(rpc, rows, seed=10) == 3
    first = [m for m in alchemy.methods if m == "eth_getTransactionReceipt"]
    assert len(first) == 3
    rows[0]["amount_raw"] = "12345"
    with alchemy.client() as rpc:
        # Checking all ten transactions must catch the altered amount.
        with pytest.raises(ChainScopeError, match="金额"):
            sample_receipts(rpc, rows, seed=10, count=10)


def test_parse_time_and_coverage_rules():
    assert parse_time("2026-10-01").isoformat() == "2026-10-01T00:00:00+08:00"
    assert parse_time("2026-10-01T08:30").tzinfo == BEIJING
    assert parse_time("2026-10-01T00:00+00:00").utcoffset().total_seconds() == 0
    with pytest.raises(ChainScopeError, match="时间格式"):
        parse_time("10/01/2026")
    job = {"start_block": 100, "verified_through": 200}
    assert coverage(job, 150, 180)["state"] == "complete"
    assert coverage(job, 150, 250)["state"] == "partial"
    assert coverage(job, 300, 400)["state"] == "none"
    assert coverage(job, 10, 50)["state"] == "complete"  # before deployment
    assert (
        coverage({"start_block": 100, "verified_through": None}, 100, 120)["state"]
        == "none"
    )


@requires_mysql
def test_reconciled_sync_marks_match_and_checks_receipts(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = Chain(30, {11: 2, 17: 1, 24: 3})
    alchemy = Alchemy(chain, finalized=24)
    with alchemy.client() as rpc:
        summary = run(engine, chain, rpc)
    # The RPC's finalized tag (24) caps the Portal's head (30).
    assert summary["source_head"] == 24
    assert summary["usage"]["rpc"]["estimated_compute_units"] > 0
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(windows.c.reconcile, windows.c.receipt_checked).order_by(
                windows.c.start_block
            )
        ).all()
    assert rows == [("match", 2), ("match", 1), ("match", 3)]
    assert job_status(engine, "bem")["reconcile"] == {"match": 3}


@requires_mysql
def test_reconcile_mismatch_records_diffs_and_holds_watermark(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = Chain(24, {11: 1, 17: 2, 24: 1})
    alchemy = Alchemy(chain, finalized=24)
    alchemy.drop.add((17, 1))
    with alchemy.client() as rpc:
        with pytest.raises(ChainScopeError, match="不一致"):
            run(engine, chain, rpc)
    assert window_rows(engine)[:2] == [(10, 14, "done"), (15, 19, "failed")]
    assert job_status(engine, "bem")["verified_through"] == 14
    with engine.connect() as connection:
        diff = connection.execute(sa.select(window_diffs)).mappings().one()
    assert diff["kind"] == "missing_in_alchemy" and diff["log_index"] == 1
    # The window's events were not committed.
    assert count(engine, transfers) == 1
    alchemy.drop.clear()
    with alchemy.client() as rpc:
        run(engine, chain, rpc)
    assert job_status(engine, "bem")["verified_through"] == 24


@requires_mysql
def test_recheck_finds_a_changed_window_and_rewinds(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = Chain(24, {11: 1, 17: 1, 24: 1})
    run(engine, chain)
    assert recheck_job(engine, "bem", chain.portal(), sample=10, notify=lambda _: None)[
        "ok"
    ]
    chain.events[18] = 1  # a late event appears in an already-done window
    result = recheck_job(
        engine, "bem", chain.portal(), sample=10, notify=lambda _: None
    )
    assert result["changed_windows"] == [{"window": [15, 19], "diffs": 1}]
    assert job_status(engine, "bem")["verified_through"] == 14
    run(engine, chain)
    assert job_status(engine, "bem")["verified_through"] == 24
    assert count(engine, transfers) == 4


@requires_mysql
def test_redo_resets_old_parser_versions_and_ranges(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = Chain(24, {11: 1})
    run(engine, chain)
    assert redo_job(engine, "bem")["windows_reset"] == 0  # all on version 1
    with engine.begin() as connection:
        connection.execute(
            windows.update().where(windows.c.start_block == 20).values(parser_version=0)
        )
    assert redo_job(engine, "bem")["windows_reset"] == 1
    assert job_status(engine, "bem")["verified_through"] == 19
    assert redo_job(engine, "bem", first=12, last=12)["windows_reset"] == 1
    assert job_status(engine, "bem")["verified_through"] is None
    run(engine, chain)
    assert job_status(engine, "bem")["verified_through"] == 24
    assert verify_job(engine, "bem")["ok"]


class TimePortal:
    """block_at for a chain whose block n has timestamp 1_700_000_000 + n."""

    def block_at(self, timestamp):
        return max(0, timestamp - 1_700_000_000)


@requires_mysql
def test_query_by_time_reports_partial_coverage(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = Chain(19, {11: 2, 17: 1})
    run(engine, chain)
    base = 1_700_000_000

    def at(n):
        from datetime import datetime

        return datetime.fromtimestamp(base + n, BEIJING).isoformat()

    full = query_job(engine, TimePortal(), "bem", at(10), at(20))
    assert full["coverage"]["state"] == "complete"
    assert full["events"] == 3 and full["range"]["blocks"] == [10, 19]
    assert full["unique_senders"] == 1
    assert full["amount_raw_sum"] == str(110 + 111 + 170)
    # Excludes the end instant: block 17 is not in [10, 17).
    assert query_job(engine, TimePortal(), "bem", at(10), at(17))["events"] == 2
    partial = query_job(engine, TimePortal(), "bem", at(15), at(40))
    assert partial["coverage"]["state"] == "partial"
    assert partial["coverage"]["covered_through_block"] == 19


@requires_mysql
def test_verify_job_report_and_full_schema_check(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    run(engine, Chain(14, {11: 2}))
    assert verify_job(engine, "bem") == {
        "job_id": "bem",
        "windows_checked": 1,
        "events": 2,
        "mismatched_windows": [],
        "ok": True,
    }
    with engine.begin() as connection:
        connection.execute(
            transfers.update().where(transfers.c.log_index == 1).values(amount_raw="7")
        )
    assert not verify_job(engine, "bem")["ok"]
    result = report(engine, "bem")
    assert {t["table"] for t in result["tables"]} >= {
        "transfer_events",
        "coverage_windows",
    }
    assert set(result["queries"]) == {
        "recent_100k_blocks",
        "one_address_history",
        "largest_20_amounts",
    }
    assert "ix_transfer_amount" in result["queries"]["largest_20_amounts"]["index_used"]
    assert check_database(engine, full=True)["schema_differences"] == []
    with engine.begin() as connection:
        connection.execute(sa.text("DROP INDEX ix_transfer_amount ON transfer_events"))
    checked = check_database(engine, full=True)
    assert checked["schema_ready"] is False
    assert any("ix_transfer_amount" in d for d in checked["schema_differences"])


@requires_mysql
def test_amounts_sort_numerically_with_the_length_column(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    run(engine, Chain(14, {11: 1}))
    with engine.begin() as connection:
        base = connection.execute(sa.select(transfers)).mappings().one()
        for i, amount in enumerate(["9", "10", "100", "99"]):
            row = {k: v for k, v in base.items() if k not in ("event_id", "amount_len")}
            row.update(tx_hash="0x" + format(900 + i, "064x"), amount_raw=amount)
            connection.execute(transfers.insert().values(**row))
        ordered = (
            connection.execute(
                sa.select(transfers.c.amount_raw)
                .where(transfers.c.amount_raw.in_(["9", "10", "100", "99"]))
                .order_by(transfers.c.amount_len, transfers.c.amount_raw)
            )
            .scalars()
            .all()
        )
    assert ordered == ["9", "10", "99", "100"]


@requires_mysql
def test_status_flags_lag_and_budget_stops(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = Chain(24, {})
    summary = run(engine, chain, max_requests=2)
    assert summary["stopped"] == "max_requests"
    status = job_status(engine, "bem", max_lag_blocks=5)
    assert status["behind"] is True
    run(engine, chain)
    assert job_status(engine, "bem", max_lag_blocks=5)["behind"] is False


@requires_mysql
@pytest.mark.parametrize("point", ["after_fetch", "before_commit", "after_commit"])
def test_crash_at_any_point_recovers_without_loss_or_duplicates(
    mysql_engine, monkeypatch, point
):
    engine = mysql_engine
    new_job(engine)
    chain = Chain(24, {11: 1, 17: 2, 24: 1})

    class Crash(BaseException):
        pass

    def exit_(code):
        raise Crash(code)

    monkeypatch.setenv("CHAINSCOPE_FAULT", point)
    monkeypatch.setattr(jobs_module.os, "_exit", exit_)
    with pytest.raises(Crash):
        run(engine, chain)
    # A real crash would also drop the MySQL session holding the job lock.
    engine.dispose()
    monkeypatch.delenv("CHAINSCOPE_FAULT")
    summary = run(engine, chain)
    expected_recovered = 0 if point == "after_commit" else 1
    assert summary["recovered_abandoned"] == expected_recovered
    assert job_status(engine, "bem")["verified_through"] == 24
    assert count(engine, transfers) == 4
    assert verify_job(engine, "bem")["ok"]


def test_topic_constant_is_standard():
    assert TRANSFER_TOPIC.startswith("0xddf252ad")
    assert FROM != TO
