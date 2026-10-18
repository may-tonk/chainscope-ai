"""Known source-version conflicts: admission guards and read-only screening."""

from copy import deepcopy

import pytest
import sqlalchemy as sa
from conftest import requires_mysql
from test_acceptance import JOB, ReadRpc, populate
from test_hybrid import Paged
from test_jobs import TOKEN, Chain

import chainscope.transaction_checks as checks
from chainscope.errors import ReconcileMismatch, SourceUnavailable
from chainscope.hybrid import sync_hybrid
from chainscope.jobs import create_job
from chainscope.models import transfers, window_diffs, window_events, windows


def test_multiple_logs_are_allowed_but_multiple_blocks_are_rejected():
    rows = [
        dict(tx_hash="a", block_number=10, log_index=1),
        dict(tx_hash="a", block_number=10, log_index=2),
    ]
    assert checks.block_conflicts(rows) == []
    conflict = checks.block_conflicts(rows, [dict(tx_hash="a", block_number=11)])
    assert conflict[0]["detail"]["block_numbers"] == [10, 11]
    assert checks.block_conflicts(rows, [dict(tx_hash="b", block_number=11)]) == []


def test_guard_receipts_are_bounded_and_provider_failure_still_quarantines(monkeypatch):
    diffs = [
        dict(
            kind="transaction_block_conflict",
            tx_hash="0x" + format(i, "064x"),
            detail=dict(block_numbers=[10, 11]),
        )
        for i in range(12)
    ]
    monkeypatch.setattr(checks, "transaction_conflicts", lambda *_: deepcopy(diffs))

    class Receipts:
        calls = 0
        fail = False

        def call(self, method, params):
            self.calls += 1
            if self.fail:
                raise SourceUnavailable("source unavailable")
            return dict(
                transactionHash=params[0], blockNumber="0xb", blockHash="0x" + "1" * 64
            )

    rpc = Receipts()
    with pytest.raises(ReconcileMismatch) as result:
        checks.reject_conflicting_transactions(None, 56, [], rpc)
    assert rpc.calls == 10 and len(result.value.diffs) == 12
    rpc.fail = True
    with pytest.raises(ReconcileMismatch) as result:
        checks.reject_conflicting_transactions(None, 56, [], rpc)
    assert result.value.diffs[0]["detail"]["receipt_error"] == "SourceUnavailable"


class Versions(Paged):
    def __init__(self, chain, across):
        super().__init__(chain)
        self.across = across

    def transfers(self, start, end):
        rows = super().transfers(start, end)
        canonical = super().transfers(11, 11)[0]
        if self.across and start <= 16 <= end:
            duplicate = deepcopy(canonical)
            duplicate["blockNum"] = hex(16)
            duplicate["uniqueId"] = duplicate["hash"] + ":log:1000"
            rows.append(duplicate)
        elif not self.across and start <= 10 <= end:
            duplicate = deepcopy(canonical)
            duplicate["blockNum"] = hex(10)
            duplicate["uniqueId"] = duplicate["hash"] + ":log:1000"
            rows.insert(0, duplicate)
        return rows


@requires_mysql
@pytest.mark.parametrize("across", [False, True])
def test_sync_quarantines_window_or_cross_window_conflict(mysql_engine, across):
    job = "transaction_guard_case"
    create_job(
        mysql_engine,
        job,
        TOKEN,
        source="alchemy",
        start_block=10,
        window_size=5,
        parser_version=1,
        decimals=18,
    )
    chain = Chain(19 if across else 14, {11: 2, 16: 1} if across else {11: 2})
    result = sync_hybrid(
        mysql_engine,
        job,
        chain.portal(),
        Versions(chain, across).client(),
        notify=lambda _: None,
    )
    assert result["quarantined_remaining"] == 1
    with mysql_engine.connect() as c:
        evidence = (
            c.execute(
                sa.select(window_diffs).where(
                    window_diffs.c.job_id == job,
                    window_diffs.c.kind == "transaction_block_conflict",
                )
            )
            .mappings()
            .all()
        )
        assert evidence[0]["detail"]["receipt_block_number"] == 11
        count = c.scalar(sa.select(sa.func.count()).select_from(transfers))
        assert count == (2 if across else 0)
        failed = (
            c.execute(
                sa.select(windows).where(
                    windows.c.job_id == job, windows.c.status == "failed"
                )
            )
            .mappings()
            .one()
        )
        assert failed["start_block"] == (15 if across else 10)


@requires_mysql
def test_transaction_commit_guard_blocks_even_if_early_checks_miss(
    mysql_engine, monkeypatch
):
    import chainscope.hybrid as hybrid

    job = "transaction_final_guard"
    create_job(
        mysql_engine,
        job,
        TOKEN,
        source="alchemy",
        start_block=10,
        window_size=5,
        parser_version=1,
        decimals=18,
    )
    chain = Chain(14, {11: 2})
    monkeypatch.setattr(checks, "reject_conflicting_transactions", lambda *_: None)
    monkeypatch.setattr(hybrid, "sample_receipts", lambda *_: 0)
    result = sync_hybrid(
        mysql_engine,
        job,
        chain.portal(),
        Versions(chain, False).client(),
        notify=lambda _: None,
    )
    assert result["quarantined_remaining"] == 1
    with mysql_engine.connect() as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(transfers)) == 0
        assert (
            c.scalar(sa.select(window_diffs.c.kind).where(window_diffs.c.job_id == job))
            == "transaction_block_conflict"
        )


class SuspectRpc(ReadRpc):
    def call(self, method, params):
        if method == "eth_getTransactionReceipt":
            self.requests += 1
            self.calls[method] += 1
            return dict(
                transactionHash=params[0],
                blockNumber=hex(11),
                blockHash=self.header(11)["hash"],
                status="0x1",
                logs=self.logs(11),
            )
        return super().call(method, params)


def add_conflict(engine):
    with engine.begin() as c:
        row = dict(
            c.execute(
                sa.select(transfers).where(
                    transfers.c.chain_id == 56, transfers.c.block_number == 11
                )
            )
            .mappings()
            .one()
        )
        row.pop("event_id")
        row.pop("amount_len")
        row.update(block_number=16, log_index=1000)
        inserted = c.execute(transfers.insert().values(**row))
        c.execute(
            window_events.insert().values(
                job_id=JOB, start_block=15, event_id=inserted.inserted_primary_key[0]
            )
        )


@requires_mysql
def test_readonly_scan_finds_cross_window_conflict_and_compares_exact_amounts(
    mysql_engine,
):
    engine, rpc = mysql_engine, SuspectRpc()
    populate(engine, rpc)
    add_conflict(engine)
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        report = checks.scan_transaction_blocks(engine, rpc, JOB, 24)
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)
    assert all(
        s.lstrip().upper().startswith(("SELECT", "START TRANSACTION"))
        for s in statements
    )
    assert report["events_scanned"] == 7 and report["candidate_count"] == 1
    assert report["verification_complete"] and not report["ok"]
    candidate = report["candidates"][0]
    assert candidate["window_starts"] == [10, 15]
    assert candidate["proposed_event_count_delta"] == -1
    assert len(candidate["comparison"]["extra_in_database"]) == 1
    assert set(candidate["proposed_balance_deltas_raw"].values()) == {
        str(10**50),
        str(-(10**50)),
    }
    assert rpc.calls["eth_getTransactionReceipt"] == 1
    assert rpc.requests == 3


@requires_mysql
def test_no_candidates_need_no_rpc_and_zero_budget_does_not_guess(mysql_engine):
    engine, rpc = mysql_engine, SuspectRpc()
    populate(engine, rpc)
    report = checks.scan_transaction_blocks(engine, rpc, JOB, 24)
    assert report["ok"] and report["candidate_count"] == 0 and rpc.requests == 0
    add_conflict(engine)
    report = checks.scan_transaction_blocks(engine, rpc, JOB, 24, max_receipts=0)
    assert report["local_scan_complete"] and not report["verification_complete"]
    assert report["candidates"][0]["status"] == "not_checked" and rpc.requests == 0


@requires_mysql
def test_transaction_check_allows_legal_logs_and_detects_stored_different_block(
    mysql_engine,
):
    engine, rpc = mysql_engine, SuspectRpc()
    populate(engine, rpc)
    tx = rpc.logs(11)[0]["transactionHash"]
    with engine.connect() as c:
        assert (
            checks.transaction_conflicts(
                c,
                56,
                [
                    dict(tx_hash=tx, block_number=11, log_index=7000),
                    dict(tx_hash=tx, block_number=11, log_index=7001),
                ],
            )
            == []
        )
        assert checks.transaction_conflicts(
            c, 56, [dict(tx_hash=tx, block_number=16, log_index=7000)]
        )[0]["detail"]["block_numbers"] == [11, 16]
        assert (
            checks.transaction_conflicts(
                c, 1, [dict(tx_hash=tx, block_number=16, log_index=7000)]
            )
            == []
        )


@requires_mysql
def test_unlinked_historical_versions_do_not_block_recovered_transactions(mysql_engine):
    engine, rpc = mysql_engine, SuspectRpc()
    populate(engine, rpc)
    add_conflict(engine)
    with engine.begin() as c:
        stale_id = c.scalar(
            sa.select(transfers.c.event_id).where(
                transfers.c.chain_id == 56, transfers.c.block_number == 16
            )
        )
        c.execute(window_events.delete().where(window_events.c.event_id == stale_id))
    with engine.connect() as c:
        assert (
            checks.transaction_conflicts(
                c,
                56,
                [dict(tx_hash=rpc.logs(11)[0]["transactionHash"], block_number=11)],
                JOB,
            )
            == []
        )
