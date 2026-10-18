"""Acceptance of pending isolation, page recovery and dual-source promotion."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from conftest import requires_mysql
from test_jobs import TOKEN, Chain, chain_block, count, new_job
from test_reconcile import Alchemy

import chainscope.hybrid as hybrid
import chainscope.ingestion as ingest
import chainscope.jobs as job_module
from chainscope.database import utcnow
from chainscope.errors import (
    ChainScopeError,
    EventConflict,
    ReconcileMismatch,
    SourceUnavailable,
)
from chainscope.holders import top_holders
from chainscope.hybrid import sync_hybrid
from chainscope.jobs import job_status, load_job, plan_windows, verify_job
from chainscope.models import (
    ingestion,
    pending_transfers,
    transfers,
    window_diffs,
    window_events,
    windows,
)
from chainscope.query import coverage, summarize
from chainscope.rpc_client import RpcClient


class Paged(Alchemy):
    def __init__(self, chain):
        super().__init__(chain, finalized=chain.head)
        self.parameters = []
        self.repeat = False
        self.quota = False

    def handler(self, request):
        body = json.loads(request.content)
        if (
            body["method"] == "eth_getBlockByNumber"
            and body["params"][0] != "finalized"
        ):
            n = int(body["params"][0], 16)
            h = chain_block(n, 0)["header"]
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": dict(h, number=hex(n), timestamp=hex(h["timestamp"])),
                },
            )
        if body["method"] != "alchemy_getAssetTransfers":
            return super().handler(request)
        p = body["params"][0]
        self.parameters.append(p)
        if self.quota:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {
                        "code": -32005,
                        "message": "quota exceeded secret-api-key",
                    },
                },
            )
        all_rows = self.transfers(int(p["fromBlock"], 16), int(p["toBlock"], 16))
        offset = int(p.get("pageKey", "0"))
        result = {"transfers": all_rows[offset : offset + 2]}
        if offset + 2 < len(all_rows):
            result["pageKey"] = "2" if self.repeat else str(offset + 2)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )


def run(engine, chain, alchemy=None, **kwargs):
    return sync_hybrid(
        engine,
        "bem",
        chain.portal(),
        (alchemy or Paged(chain)).client(),
        notify=lambda _: None,
        sleep=lambda _: None,
        **kwargs,
    )


def only_window(engine):
    with engine.connect() as c:
        job = load_job(c, "bem")
        window = dict(
            c.execute(sa.select(windows).order_by(windows.c.start_block))
            .mappings()
            .first()
        )
    return job, window


@requires_mysql
def test_collect_never_admits_pending_to_query_or_balances(mysql_engine):
    e = mysql_engine
    new_job(e)
    chain = Chain(19, {11: 3, 18: 2})
    chain.fail_from = 10  # Even an unavailable Portal cannot block the primary.
    result = run(e, chain, phase="collect")
    assert result["collected_through"] == 19
    assert result["pending_events_saved"] == 5
    assert result["verified_through"] is None and not result["coverage_complete"]
    assert count(e, pending_transfers) == 5
    assert count(e, transfers) == count(e, window_events) == 0
    with e.connect() as c:
        job = load_job(c, "bem")
    assert coverage(job, 10, 19)["state"] == "none"
    assert summarize(e, job, 10, 19)["events"] == 0
    with pytest.raises(ChainScopeError):
        top_holders(e, "bem")
    assert chain.requests == []


@requires_mysql
def test_supplement_promotes_and_retry_does_not_repeat_primary(mysql_engine):
    e = mysql_engine
    new_job(e)
    chain = Chain(19, {11: 3, 18: 2})
    a = Paged(chain)
    run(e, chain, a, phase="collect")
    before = len(a.parameters)
    result = run(e, chain, a, phase="supplement")
    assert result["coverage_complete"] and result["verified_through"] == 19
    assert len(a.parameters) == before
    assert count(e, transfers) == 5 and count(e, window_events) == 5
    assert result["awaiting_verification_events"] == 0
    assert verify_job(e, "bem")["ok"]
    again = run(e, chain, a)
    assert again["windows_done"] == again["windows_collected"] == 0
    assert len(a.parameters) == before


@requires_mysql
@pytest.mark.parametrize(
    "point,committed", [("before_page_commit", False), ("after_page_commit", True)]
)
def test_page_rows_and_cursor_commit_together(
    mysql_engine, monkeypatch, point, committed
):
    e = mysql_engine
    new_job(e)
    plan_windows(e, "bem", 14)
    job, window = only_window(e)
    a = Paged(Chain(14, {11: 5}))

    def fail(name):
        if name == point:
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(ingest, "fault", fail)
    with pytest.raises(RuntimeError):
        ingest.collect_window(e, job, window, a.client())
    with e.connect() as c:
        saved = ingest.checkpoint(c, "bem", 10)
    assert saved["pages"] == int(committed)
    assert count(e, pending_transfers) == (2 if committed else 0)
    monkeypatch.setattr(ingest, "fault", lambda _: None)
    previous = len(a.parameters)
    ingest.collect_window(e, job, window, a.client())
    assert a.parameters[previous].get("pageKey") == ("2" if committed else None)
    assert count(e, pending_transfers) == 5 and count(e, transfers) == 0


@requires_mysql
def test_expired_cursor_restarts_only_its_window_and_retains_evidence(mysql_engine):
    e = mysql_engine
    new_job(e)
    plan_windows(e, "bem", 14)
    job, window = only_window(e)
    a = Paged(Chain(14, {11: 5}))
    rpc = a.client()
    ingest.collect_window(e, job, window, rpc, stop=lambda: rpc.requests >= 1)
    with e.begin() as c:
        old = ingest.checkpoint(c, "bem", 10)["generation"]
        c.execute(
            ingestion.update().values(updated_at_utc=utcnow() - timedelta(minutes=11))
        )
    ingest.collect_window(e, job, window, a.client())
    assert a.parameters[1].get("pageKey") is None
    with e.connect() as c:
        assert ingest.checkpoint(c, "bem", 10)["generation"] != old
    assert len(ingest.pending_rows(e, "bem", window)) == 5
    assert count(e, pending_transfers) == 5


@requires_mysql
def test_duplicate_cursor_stops_without_false_completion(mysql_engine):
    e = mysql_engine
    new_job(e)
    plan_windows(e, "bem", 14)
    job, window = only_window(e)
    a = Paged(Chain(14, {11: 6}))
    a.repeat = True
    with pytest.raises(ChainScopeError, match="游标"):
        ingest.collect_window(e, job, window, a.client())
    with e.connect() as c:
        assert not ingest.checkpoint(c, "bem", 10)["complete"]
    assert count(e, pending_transfers) == 2 and count(e, transfers) == 0


@requires_mysql
def test_staged_conflict_is_not_overwritten(mysql_engine):
    e = mysql_engine
    new_job(e)
    plan_windows(e, "bem", 14)
    job, window = only_window(e)
    saved = ingest.prepare(e, "bem", window)
    raw = Paged(Chain(14, {11: 1})).transfers(10, 14)[0]
    row = ingest.parse_pending(raw, job, window)
    saved = ingest.save_page(e, "bem", window, saved, [row], "next")
    with pytest.raises(EventConflict):
        ingest.save_page(e, "bem", window, saved, [dict(row, amount_raw="999")], None)
    with e.connect() as c:
        assert c.scalar(sa.select(pending_transfers.c.amount_raw)) == row["amount_raw"]
        assert not ingest.checkpoint(c, "bem", 10)["complete"]


@requires_mysql
def test_sqd_failure_does_not_block_primary_admission(mysql_engine):
    e = mysql_engine
    new_job(e)
    chain = Chain(19, {11: 2, 18: 2})
    chain.fail_from = 10
    result = run(e, chain)
    assert result["stopped"] == "awaiting_supplement"
    assert result["collected_through"] == result["verified_through"] == 19
    assert result["pending_events_saved"] == 0
    assert not result["coverage_complete"] and count(e, transfers) == 4
    assert result["transfers_complete"] and not result["swaps_complete"]


@requires_mysql
def test_primary_does_not_claim_full_independent_verification(mysql_engine):
    e = mysql_engine
    new_job(e)
    chain = Chain(14, {11: 3})
    a = Paged(chain)
    a.drop.add((11, 1))
    result = run(e, chain, a)
    assert result["quarantined_remaining"] == 0
    assert result["verified_through"] == 14
    assert count(e, transfers) == 2 and count(e, pending_transfers) == 2
    with e.connect() as c:
        assert c.scalar(sa.select(windows.c.reconcile)) == "local_checked"
    again = run(e, chain, a)
    assert again["quarantined_remaining"] == 0 and again["coverage_complete"]


@requires_mysql
def test_discovery_resumes_from_committed_chunk(mysql_engine, monkeypatch):
    e = mysql_engine
    new_job(e)
    chain = Chain(24, {11: 2, 18: 2, 23: 1})
    monkeypatch.setattr(hybrid, "DISCOVERY_CHUNK", 5)
    run(e, chain, phase="collect")
    chain.fail_from = 15
    partial = run(e, chain, phase="supplement")
    assert partial["pool_discovery_through"] == 14 and partial["verified_through"] == 24
    assert partial["swaps_verified_through"] == 14
    assert count(e, transfers) == 5
    chain.fail_from = None
    chain.requests.clear()
    result = run(e, chain, phase="supplement")
    assert result["coverage_complete"] and count(e, transfers) == 5
    assert all(start >= 15 for start, _ in chain.requests)


@requires_mysql
def test_quota_fallback_stays_pending_until_other_provider_returns(mysql_engine):
    e = mysql_engine
    new_job(e)
    chain = Chain(14, {11: 3})
    a = Paged(chain)
    a.quota = True
    result = run(e, chain, a, phase="collect")
    assert result["source_switches"] and result["collected_through"] == 14
    with e.connect() as c:
        assert ingest.checkpoint(c, "bem", 10)["provider"] == "sqd"
    assert count(e, transfers) == 0
    pending = run(e, chain, a, phase="supplement")
    assert not pending["coverage_complete"] and count(e, transfers) == 0
    a.quota = False
    checked = run(e, chain, a, phase="supplement")
    assert checked["coverage_complete"] and count(e, transfers) == 3


@requires_mysql
def test_primary_splits_dense_windows_and_retains_prior_progress(mysql_engine):
    e = mysql_engine
    new_job(e)
    chain = Chain(19, {11: 5, 18: 2})
    result = run(e, chain, phase="collect", event_limit=3, block_hard_limit=6)
    assert result["splits"] > 0 and result["collected_through"] == 19
    assert result["pending_events_saved"] == 7
    assert count(e, transfers) == 0
    checked = run(e, chain, event_limit=3, block_hard_limit=6, phase="supplement")
    assert checked["coverage_complete"] and count(e, transfers) == 7


@requires_mysql
def test_old_verified_data_is_preserved_while_new_pages_wait(mysql_engine):
    from chainscope.sync import sync_job

    e = mysql_engine
    new_job(e)
    chain = Chain(14, {11: 2})
    sync_job(e, "bem", chain.portal(), rpc=Paged(chain).client(), notify=lambda _: None)
    with e.connect() as c:
        before = list(c.execute(sa.select(transfers)).mappings())
    chain.head = 19
    chain.events[18] = 3
    result = run(e, chain, phase="collect")
    assert result["verified_through"] == 14 and result["collected_through"] == 19
    with e.connect() as c:
        assert list(c.execute(sa.select(transfers)).mappings()) == before
        job = load_job(c, "bem")
    assert summarize(e, job, 10, 19)["events"] == 2
    assert run(e, chain, phase="supplement")["coverage_complete"]
    assert count(e, transfers) == 5


@requires_mysql
def test_fixed_cutoff_and_later_manual_increment(mysql_engine):
    e = mysql_engine
    new_job(e)
    chain = Chain(24, {11: 2, 18: 1, 23: 2})
    result = run(e, chain, until_block=14)
    assert result["target_block"] == result["verified_through"] == 14
    with e.connect() as c:
        assert c.scalar(sa.select(sa.func.max(windows.c.end_block))) == 14
    later = run(e, chain)
    assert later["verified_through"] == 24 and count(e, transfers) == 5


def test_quota_errors_are_safe_and_bad_parameters_do_not_trigger_fallback():
    for code, message, expected in [
        (-32005, "quota exceeded secret-api-key", SourceUnavailable),
        (-32602, "invalid params secret-api-key", ChainScopeError),
    ]:

        def handler(request):
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": code, "message": message},
                },
            )

        with RpcClient(
            "https://example.invalid/key",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ) as rpc:
            with pytest.raises(expected) as error:
                rpc.call("alchemy_getAssetTransfers", [{}])
            assert "secret-api-key" not in str(error.value)
            if code == -32602:
                assert not isinstance(error.value, SourceUnavailable)


@requires_mysql
def test_promotion_crash_preserves_pending_and_recovers_without_refetch(
    mysql_engine, monkeypatch
):
    from chainscope.jobs import create_job

    e = mysql_engine
    name = "hybrid_commit_test"
    create_job(
        e,
        name,
        TOKEN,
        source="alchemy",
        start_block=10,
        window_size=5,
        parser_version=1,
        decimals=18,
    )
    chain = Chain(14, {11: 3})
    a = Paged(chain)

    def execute(phase):
        return sync_hybrid(
            e, name, chain.portal(), a.client(), phase=phase, notify=lambda _: None
        )

    execute("collect")
    calls = len(a.parameters)

    def crash(point):
        if point == "before_commit":
            raise RuntimeError("simulated process crash")

    monkeypatch.setattr(job_module, "fault", crash)
    with pytest.raises(RuntimeError):
        execute("supplement")
    assert count(e, transfers) == 0 and count(e, pending_transfers) == 3
    assert job_status(e, name)["verified_through"] is None
    monkeypatch.setattr(job_module, "fault", lambda _: None)
    recovered = execute("supplement")
    assert recovered["recovered_abandoned"] == 1
    assert recovered["coverage_complete"] and count(e, transfers) == 3
    assert len(a.parameters) == calls


@requires_mysql
@pytest.mark.parametrize("crash", [False, True])
def test_hybrid_discovers_swaps_and_verifies_their_receipts(
    mysql_engine, monkeypatch, crash
):
    from test_dex import CHAIN, POOL, SwapChain

    from chainscope.jobs import create_job
    from chainscope.models import swaps
    from chainscope.sqd import block_transfers

    class SwapAlchemy(Alchemy):
        def transfers(self, start, end):
            found = []
            for number, block in CHAIN.items():
                if start <= number <= end:
                    for event in block_transfers(block, TOKEN, ignore={POOL}):
                        found.append(
                            {
                                "category": "erc20",
                                "blockNum": hex(number),
                                "hash": event["tx_hash"],
                                "uniqueId": f"{event['tx_hash']}:log:{event['log_index']}",
                                "from": event["from_address"],
                                "to": event["to_address"],
                                "rawContract": {
                                    "value": hex(event["amount"]),
                                    "address": TOKEN,
                                },
                            }
                        )
            return found

        def receipt(self, tx_hash):
            for number, block in CHAIN.items():
                logs = [
                    dict(
                        log,
                        logIndex=hex(log["logIndex"]),
                        blockNumber=hex(number),
                        blockHash=block["header"]["hash"],
                        removed=False,
                    )
                    for log in block["logs"]
                    if log["transactionHash"] == tx_hash
                ]
                if logs:
                    sender = next(
                        tx["from"]
                        for tx in block["transactions"]
                        if tx["hash"] == tx_hash
                    )
                    return {
                        "status": "0x1",
                        "from": sender,
                        "transactionHash": tx_hash,
                        "blockNumber": hex(number),
                        "blockHash": block["header"]["hash"],
                        "logs": logs,
                    }
            return None

    e = mysql_engine
    name = "hybrid_swap_test"
    create_job(
        e,
        name,
        TOKEN,
        source="alchemy",
        start_block=10,
        window_size=5,
        parser_version=1,
        decimals=18,
    )
    chain = SwapChain()
    source = SwapAlchemy(chain, finalized=24)
    rpc = source.client()
    if crash:

        def stop(point):
            if point == "before_swap_commit":
                raise RuntimeError("simulated Swap transaction crash")

        monkeypatch.setattr(job_module, "fault", stop)
        with pytest.raises(RuntimeError):
            sync_hybrid(e, name, chain.portal(), rpc, notify=lambda _: None)
        assert count(e, transfers) == 8 and count(e, swaps) == 0
        status = job_status(e, name, head=24)
        assert (
            status["verified_through"] == 24
            and status["swaps_verified_through"] is None
        )
        calls = source.methods.count("alchemy_getAssetTransfers")
        monkeypatch.setattr(job_module, "fault", lambda _: None)
    result = sync_hybrid(e, name, chain.portal(), rpc, notify=lambda _: None)
    assert result["coverage_complete"] and not result["quarantined"]
    assert count(e, transfers) == 8 and count(e, swaps) == 4
    assert verify_job(e, name)["ok"]
    assert all(
        not (
            q.get("fields", {}).get("log")
            and any(TOKEN in f["address"] for f in q["logs"])
        )
        for q in chain.queries
    )
    if crash:
        assert source.methods.count("alchemy_getAssetTransfers") == calls
    from test_dex import JUNK

    from chainscope.dex import set_pool_enabled
    from chainscope.query import swap_summary

    set_pool_enabled(e, name, JUNK, True)
    status = job_status(e, name, head=24)
    assert status["verified_through"] == 24
    assert status["swaps_verified_through"] is None
    with e.connect() as c:
        job = load_job(c, name)
    assert swap_summary(e, job, 10, 24)["coverage"]["state"] == "none"
    assert sum(r["swaps"] for r in swap_summary(e, job, 10, 24)["by_side"]) == 0
    before = source.methods.count("alchemy_getAssetTransfers")
    checked = sync_hybrid(e, name, chain.portal(), rpc, notify=lambda _: None)
    assert checked["swaps_complete"] and checked["transfers_complete"]
    assert source.methods.count("alchemy_getAssetTransfers") == before
    assert count(e, transfers) == 8 and verify_job(e, name)["ok"]


@requires_mysql
def test_primary_window_is_formal_before_full_history_finishes(mysql_engine):
    e = mysql_engine
    new_job(e)
    chain = Chain(19, {11: 2, 18: 3})
    result = run(e, chain, max_windows=1)
    assert result["verified_through"] == 14
    assert not result["transfers_complete"] and not result["swaps_complete"]
    assert count(e, transfers) == 2
    with e.connect() as c:
        job = load_job(c, "bem")
    assert summarize(e, job, 10, 14)["events"] == 2
    assert top_holders(e, "bem")["as_of_block"] == 14


@requires_mysql
def test_bad_staged_count_blocks_formal_admission(mysql_engine):
    e = mysql_engine
    new_job(e)
    chain = Chain(14, {11: 2})
    run(e, chain, phase="collect")
    with e.begin() as c:
        c.execute(ingestion.update().values(event_count=3))
    with pytest.raises(ChainScopeError, match="数量"):
        run(e, chain)
    assert count(e, transfers) == 0


class Timestamped(Paged):
    """Transfer metadata differs while the block header stays authoritative."""

    def __init__(self, chain, *, bad_header=None, bad_receipt=False):
        super().__init__(chain)
        self.bad_header = bad_header
        self.bad_receipt = bad_receipt
        self.header_calls = 0

    def transfers(self, start, end):
        rows = super().transfers(start, end)
        for row in rows:
            number = int(row["blockNum"], 16)
            stamp = chain_block(number, 0)["header"]["timestamp"] - 2
            row["metadata"] = {
                "blockTimestamp": datetime.fromtimestamp(stamp, UTC).isoformat()
            }
        return rows

    def receipt(self, tx_hash):
        receipt = super().receipt(tx_hash)
        if self.bad_receipt:
            receipt["blockHash"] = "0x" + "f" * 64
        return receipt

    def handler(self, request):
        body = json.loads(request.content)
        is_header = (
            body["method"] == "eth_getBlockByNumber"
            and body["params"][0] != "finalized"
        )
        if is_header:
            self.header_calls += 1
            if self.bad_header == "unavailable":
                return httpx.Response(503)
        response = super().handler(request)
        if is_header and self.bad_header:
            content = response.json()
            if self.bad_header == "missing":
                content["result"] = None
            else:
                field, value = {
                    "hash": ("hash", "0x" + "f" * 64),
                    "time": ("timestamp", "0x1"),
                    "number": ("number", "0x1"),
                }[self.bad_header]
                content["result"][field] = value
            return httpx.Response(200, json=content)
        return response


@requires_mysql
@pytest.mark.parametrize("crash", [False, True])
def test_confirmed_header_time_admits_without_refetch_and_keeps_evidence(
    mysql_engine, monkeypatch, crash
):
    e = mysql_engine
    new_job(e)
    chain = Chain(14, {11: 3})
    a = Timestamped(chain)
    run(e, chain, a, phase="collect")
    before_pages = len(a.parameters)
    with e.connect() as c:
        original = list(c.execute(sa.select(pending_transfers)).mappings())
    job, window = only_window(e)
    hybrid.quarantine(
        e,
        job,
        window,
        ReconcileMismatch("old timestamp mismatch", [{"kind": "header_time_conflict"}]),
    )
    job_module.reset_windows(e, "bem", first=10, last=14)
    if crash:

        def fail(point):
            if point == "before_commit":
                raise RuntimeError("crash before time evidence commit")

        monkeypatch.setattr(job_module, "fault", fail)
        with pytest.raises(RuntimeError):
            run(e, chain, a)
        assert count(e, transfers) == 0
        # Only the original quarantine evidence survives the rolled-back commit.
        assert count(e, window_diffs) == 1
        monkeypatch.setattr(job_module, "fault", lambda _: None)
    result = run(e, chain, a)
    assert result["coverage_complete"] and result["verified_through"] == 14
    assert len(a.parameters) == before_pages
    assert a.header_calls == (2 if crash else 1)  # one RPC per differing block
    with e.connect() as c:
        assert list(c.execute(sa.select(pending_transfers)).mappings()) == original
        events = list(c.execute(sa.select(transfers)).mappings())
        audit = list(c.execute(sa.select(window_diffs)).mappings())
    assert len(events) == 3 and len(audit) == 4
    for row in events:
        source = next(r for r in original if r["tx_hash"] == row["tx_hash"])
        assert row["block_time_utc"] == source["block_time_utc"] + timedelta(seconds=2)
        for key in (
            "tx_hash",
            "log_index",
            "block_number",
            "from_address",
            "to_address",
            "amount_raw",
        ):
            assert row[key] == source[key]
    resolved = [r for r in audit if r["kind"] == "header_time_confirmed"]
    assert len(resolved) == 3
    assert (
        resolved[0]["detail"]["source_time_utc"]
        == original[0]["block_time_utc"].isoformat()
    )
    assert verify_job(e, "bem")["ok"]
    assert run(e, chain, a)["windows_done"] == 0
    assert count(e, window_diffs) == 4  # rerunning does not duplicate evidence


@requires_mysql
@pytest.mark.parametrize(
    "bad_header", ["hash", "time", "missing", "number", "unavailable"]
)
def test_time_correction_requires_matching_live_block_header(mysql_engine, bad_header):
    e = mysql_engine
    new_job(e)
    chain = Chain(14, {11: 2})
    a = Timestamped(chain, bad_header=bad_header)
    run(e, chain, a, phase="collect")
    if bad_header in ("missing", "number"):
        with pytest.raises(ChainScopeError, match="区块头缺失或高度不符"):
            run(e, chain, a)
    else:
        result = run(e, chain, a)
        assert not result["transfers_complete"]
        if bad_header != "unavailable":
            assert result["quarantined_remaining"] == 1
    assert count(e, transfers) == 0
    assert count(e, pending_transfers) == 2
    with e.connect() as c:
        assert (
            c.scalar(
                sa.select(sa.func.count())
                .select_from(window_diffs)
                .where(window_diffs.c.kind == "header_time_confirmed")
            )
            == 0
        )


@requires_mysql
def test_time_correction_does_not_bypass_affected_receipt_check(mysql_engine):
    e = mysql_engine
    new_job(e)
    chain = Chain(14, {11: 2})
    result = run(e, chain, Timestamped(chain, bad_receipt=True))
    assert result["quarantined_remaining"] == 1
    assert count(e, transfers) == 0
    with e.connect() as c:
        assert c.scalar(sa.select(window_diffs.c.kind)) == "receipt_mismatch"
