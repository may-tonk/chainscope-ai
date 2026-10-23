"""Receipt recovery preserves evidence and checkpoint correctness on failures."""

import json

import httpx
import pytest
import sqlalchemy as sa
from conftest import requires_mysql
from test_hybrid import Paged
from test_jobs import TOKEN, Chain, count

import chainscope.jobs as job_state
from chainscope.errors import ChainScopeError, ReconcileMismatch
from chainscope.hybrid import quarantine, sync_hybrid
from chainscope.jobs import create_job, load_job, verify_job
from chainscope.models import (
    ingestion,
    pending_transfers,
    transfers,
    window_diffs,
    windows,
)
from chainscope.recovery import PENDING_FIELDS, receipt_transfers, recover_receipts

NAME = "receipt_recovery_case"


class Drifted(Paged):
    def __init__(self, chain, *, same_index=False, bad=None, extra=False):
        super().__init__(chain)
        self.same_index = same_index
        self.bad = bad
        self.extra = extra

    def transfers(self, start, end):
        rows = super().transfers(start, end)
        if rows:
            rows[0]["blockNum"] = hex(10)
            if not self.same_index:
                rows[0]["uniqueId"] = rows[0]["hash"] + ":log:100"
            rows[0]["rawContract"]["value"] = hex(111)
        return rows

    def receipt(self, tx_hash):
        row = super().receipt(tx_hash)
        if self.bad == "missing":
            return None
        if self.bad == "outside":
            row["blockNumber"] = hex(99)
        if self.bad == "failed":
            row["status"] = "0x0"
        if self.bad == "log_hash":
            row["logs"][0]["blockHash"] = "0x" + "f" * 64
        if self.extra:
            row["logs"].append(
                dict(row["logs"][0], logIndex=hex(99), data="0x" + format(500, "064x"))
            )
        return row

    def handler(self, request):
        response = super().handler(request)
        body = json.loads(request.content)
        if (
            self.bad == "header"
            and body["method"] == "eth_getBlockByNumber"
            and body["params"][0] != "finalized"
        ):
            content = response.json()
            content["result"]["hash"] = "0x" + "f" * 64
            return httpx.Response(200, json=content)
        return response


def prepared(e, **kwargs):
    create_job(
        e,
        NAME,
        TOKEN,
        source="alchemy",
        start_block=10,
        window_size=5,
        parser_version=1,
        decimals=18,
    )
    chain = Chain(14, {11: 2})
    source = Drifted(chain, **kwargs)
    sync_hybrid(
        e, NAME, chain.portal(), source.client(), phase="collect", notify=lambda _: None
    )
    with e.connect() as c:
        job = load_job(c, NAME)
        window = dict(c.execute(sa.select(windows)).mappings().one())
        originals = [
            dict(r)
            for r in c.execute(
                sa.select(pending_transfers).order_by(
                    pending_transfers.c.log_index.desc()
                )
            ).mappings()
        ]
    tx = next(
        r["tx_hash"]
        for r in originals
        if r["amount_raw"] == "111" and r["block_number"] == 10
    )
    quarantine(
        e,
        job,
        window,
        ReconcileMismatch("metadata drift", [{"kind": "header_time_conflict"}]),
    )
    return chain, source, tx


@requires_mysql
@pytest.mark.parametrize("same_index", [False, True])
def test_receipt_recovery_preserves_originals_and_does_not_repeat_pages(
    mysql_engine, same_index
):
    e = mysql_engine
    chain, source, tx = prepared(e, same_index=same_index)
    pages = len(source.parameters)
    repaired = recover_receipts(e, NAME, 10, [tx], source.client())
    assert repaired["original_events"] == repaired["replacement_events"] == 1
    assert count(e, transfers) == 0
    with e.connect() as c:
        audit = (
            c.execute(
                sa.select(window_diffs).where(
                    window_diffs.c.kind == "receipt_recovered"
                )
            )
            .mappings()
            .one()
        )
        assert audit["detail"]["original_rows"][0]["amount_raw"] == "111"
        assert audit["detail"]["original_rows"][0]["block_number"] == 10
        assert audit["detail"]["replacement_rows"][0]["amount_raw"] == "110"
        assert c.scalar(sa.select(windows.c.status)) == "pending"
    assert recover_receipts(e, NAME, 10, [tx], source.client())["already_repaired"]
    result = sync_hybrid(
        e, NAME, chain.portal(), source.client(), notify=lambda _: None
    )
    assert result["coverage_complete"] and count(e, transfers) == 2
    assert len(source.parameters) == pages
    assert count(e, window_diffs) == 2
    assert verify_job(e, NAME)["ok"]


@requires_mysql
def test_receipt_recovery_crash_rolls_back_evidence_rows_and_checkpoint(
    mysql_engine, monkeypatch
):
    e = mysql_engine
    _, source, tx = prepared(e)
    with e.connect() as c:
        before_rows = list(c.execute(sa.select(pending_transfers)).mappings())
        before_checkpoint = dict(c.execute(sa.select(ingestion)).mappings().one())

    def crash(point):
        if point == "before_receipt_repair_commit":
            raise RuntimeError("recovery crash")

    monkeypatch.setattr(job_state, "fault", crash)
    with pytest.raises(RuntimeError):
        recover_receipts(e, NAME, 10, [tx], source.client())
    with e.connect() as c:
        assert list(c.execute(sa.select(pending_transfers)).mappings()) == before_rows
        assert (
            dict(c.execute(sa.select(ingestion)).mappings().one()) == before_checkpoint
        )
        assert c.scalar(sa.select(windows.c.status)) == "failed"
    assert count(e, window_diffs) == 1 and count(e, transfers) == 0
    monkeypatch.setattr(job_state, "fault", lambda _: None)
    assert (
        recover_receipts(e, NAME, 10, [tx], source.client())["replacement_events"] == 1
    )


@requires_mysql
@pytest.mark.parametrize("bad", ["missing", "outside", "failed", "header", "log_hash"])
def test_invalid_recovery_does_not_release_or_change_rows(mysql_engine, bad):
    e = mysql_engine
    _, source, tx = prepared(e, bad=bad)
    with e.connect() as c:
        original = list(c.execute(sa.select(pending_transfers)).mappings())
    with pytest.raises(ChainScopeError):
        recover_receipts(e, NAME, 10, [tx], source.client())
    with e.connect() as c:
        assert list(c.execute(sa.select(pending_transfers)).mappings()) == original
        assert c.scalar(sa.select(windows.c.status)) == "failed"
    assert count(e, window_diffs) == 1 and count(e, transfers) == 0


@requires_mysql
def test_recovery_recomputes_count_when_receipt_has_additional_events(mysql_engine):
    e = mysql_engine
    _, source, tx = prepared(e, extra=True)
    result = recover_receipts(e, NAME, 10, [tx], source.client())
    assert result["window_events_before"] == 2 and result["window_events_after"] == 3
    assert count(e, pending_transfers) == 3
    with e.connect() as c:
        assert c.scalar(sa.select(ingestion.c.event_count)) == 3


@requires_mysql
def test_recovery_removes_stale_and_current_versions_of_the_same_transaction(
    mysql_engine,
):
    e = mysql_engine
    chain, source, tx = prepared(e)
    canonical, _ = receipt_transfers(source.client(), TOKEN, tx, 10, 14)
    with e.begin() as c:
        generation = c.scalar(sa.select(ingestion.c.generation))
        c.execute(
            pending_transfers.insert().values(
                job_id=NAME,
                start_block=10,
                generation=generation,
                **{k: canonical[0][k] for k in PENDING_FIELDS},
            )
        )
        c.execute(ingestion.update().values(event_count=3))
    result = recover_receipts(e, NAME, 10, [tx], source.client())
    assert result["original_events"] == 2 and result["replacement_events"] == 1
    assert result["window_events_after"] == 2
    with e.connect() as c:
        evidence = c.execute(
            sa.select(window_diffs.c.detail).where(
                window_diffs.c.kind == "receipt_recovered"
            )
        ).scalar_one()
    assert len(evidence["original_rows"]) == 2
    result = sync_hybrid(
        e, NAME, chain.portal(), source.client(), notify=lambda _: None
    )
    assert result["coverage_complete"] and count(e, transfers) == 2
