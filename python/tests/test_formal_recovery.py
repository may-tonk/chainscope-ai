"""Formal repair is bounded, atomic, evidence-preserving and idempotent."""

import hashlib
import json
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from conftest import requires_mysql

from chainscope import jobs as job_state
from chainscope.database import utcnow
from chainscope.errors import ChainScopeError
from chainscope.formal_recovery import repair_completed_receipt
from chainscope.jobs import create_job
from chainscope.models import (
    balances,
    ingestion,
    jobs,
    pending_transfers,
    transfers,
    window_diffs,
    window_events,
    windows,
)
from chainscope.recovery import PENDING_FIELDS
from chainscope.validator import TRANSFER_TOPIC, Tally

JOB = "formal_repair_case"
TOKEN, SENDER, RECEIVER, OTHER = ["0x" + c * 40 for c in "1234"]
ZERO = "0x" + "0" * 40
TX = "0x" + "a" * 64


def event(number, index, sender, receiver, amount, tx=TX):
    return dict(
        chain_id=56,
        token_address=TOKEN,
        token_decimals=8,
        block_number=number,
        block_hash="0x" + format(number, "064x"),
        block_time_utc=datetime.fromtimestamp(1_700_000_000 + number, UTC).replace(
            tzinfo=None
        ),
        tx_hash=tx,
        log_index=index,
        from_address=sender,
        to_address=receiver,
        amount_raw=str(amount),
    )


class Rpc:
    def __init__(self):
        self.requests = 0
        self.calls = Counter()

    def call(self, method, params):
        self.requests += 1
        self.calls[method] += 1
        if method == "eth_chainId":
            return "0x38"
        if method == "eth_getBlockByNumber":
            return dict(
                number="0xd",
                hash="0x" + format(13, "064x"),
                timestamp=hex(1_700_000_013),
            )
        if method == "eth_getTransactionReceipt":
            return dict(
                transactionHash=TX,
                status="0x1",
                blockNumber="0xd",
                blockHash="0x" + format(13, "064x"),
                logs=[
                    dict(
                        address=TOKEN,
                        topics=[
                            TRANSFER_TOPIC,
                            "0x" + "0" * 24 + SENDER[2:],
                            "0x" + "0" * 24 + RECEIVER[2:],
                        ],
                        data="0x" + format(100, "064x"),
                        logIndex="0x2",
                        transactionHash=TX,
                        blockNumber="0xd",
                        blockHash="0x" + format(13, "064x"),
                        removed=False,
                    )
                ],
            )
        if method == "eth_call":
            assert params[1] == "0xe"
            address = "0x" + params[0]["data"][-40:]
            return hex({SENDER: 850, RECEIVER: 100}[address])
        raise AssertionError(method)


def prepare(engine, tmp_path):
    create_job(
        engine,
        JOB,
        TOKEN,
        source="alchemy",
        start_block=10,
        window_size=5,
        parser_version=1,
        decimals=8,
    )
    rows = [
        event(11, 0, ZERO, SENDER, 1000, "0x" + "b" * 64),
        event(12, 1, SENDER, RECEIVER, 100),
        event(13, 2, SENDER, RECEIVER, 100),
        event(14, 3, SENDER, OTHER, 50, "0x" + "c" * 64),
    ]
    tally = Tally()
    for r in rows:
        tally.add(
            r["tx_hash"],
            r["log_index"],
            r["from_address"],
            r["to_address"],
            int(r["amount_raw"]),
            r["block_number"],
        )
    now = utcnow()
    with engine.begin() as c:
        c.execute(
            jobs.update()
            .where(jobs.c.job_id == JOB)
            .values(verified_through=14, balances_through=14)
        )
        c.execute(
            windows.insert().values(
                job_id=JOB,
                start_block=10,
                end_block=14,
                status="done",
                event_count=4,
                inserted_count=4,
                amount_sum=str(tally.amount),
                fingerprint=tally.fingerprint_bytes(),
                fingerprint_version=2,
                updated_at_utc=now,
            )
        )
        c.execute(
            ingestion.insert().values(
                job_id=JOB,
                start_block=10,
                end_block=14,
                generation="test_generation",
                provider="alchemy",
                page_key=None,
                seen_keys=[],
                pages=1,
                event_count=4,
                complete=True,
                updated_at_utc=now,
            )
        )
        for row in rows:
            result = c.execute(transfers.insert().values(**row))
            c.execute(
                window_events.insert().values(
                    job_id=JOB, start_block=10, event_id=result.inserted_primary_key[0]
                )
            )
            c.execute(
                pending_transfers.insert().values(
                    job_id=JOB,
                    start_block=10,
                    generation="test_generation",
                    **{k: row[k] for k in PENDING_FIELDS},
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
                    last_block=14,
                )
                for a, v in ((SENDER, 750), (RECEIVER, 200), (OTHER, 50))
            ],
        )
        expected = [
            dict(r)
            for r in c.execute(
                sa.select(transfers).where(transfers.c.tx_hash == TX)
            ).mappings()
        ]
    path = tmp_path / "backup.sql"
    path.write_bytes(b"-- synthetic isolated test backup\n-- Dump completed on test\n")
    backup = dict(
        path=str(path),
        database=engine.url.database,
        bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        completed=True,
        schema_revision="0008",
    )
    return dict(
        job_id=JOB,
        start=10,
        cutoff=14,
        tx_hash=TX,
        expected_rows=json.loads(json.dumps(expected, default=str)),
        expected_deltas={SENDER: "100", RECEIVER: "-100"},
        expected_window_count=4,
        expected_job_total=4,
        backup=backup,
    )


def snapshot(engine):
    with engine.connect() as c:
        return {
            table.name: [dict(r) for r in c.execute(sa.select(table)).mappings()]
            for table in (
                jobs,
                windows,
                ingestion,
                pending_transfers,
                transfers,
                window_events,
                balances,
                window_diffs,
            )
        }


@requires_mysql
def test_repair_preserves_evidence_and_repeat_does_not_double_adjust(
    mysql_engine, tmp_path
):
    plan = prepare(mysql_engine, tmp_path)
    result = repair_completed_receipt(mysql_engine, Rpc(), **plan)
    assert result["removed_records"] == 1
    assert result["formal_events_after"] == result["window_events_after"] == 3
    data = snapshot(mysql_engine)
    assert len(data["transfer_events"]) == len(data["pending_transfers"]) == 3
    assert data["transfer_ingestion"][0]["event_count"] == 3
    assert (
        data["scan_jobs"][0]["verified_through"]
        == data["scan_jobs"][0]["balances_through"]
        == 14
    )
    audit = data["window_diffs"][0]["detail"]
    assert (
        len(audit["original_formal_rows"]) == len(audit["original_pending_rows"]) == 2
    )
    assert audit["original_window"]["event_count"] == 4
    again = repair_completed_receipt(mysql_engine, Rpc(), **plan)
    assert again["already_repaired"]
    assert snapshot(mysql_engine) == data


@requires_mysql
@pytest.mark.parametrize(
    "point", ["after_formal_repair_deletes", "before_formal_repair_commit"]
)
def test_crash_rolls_back_every_changed_table(
    mysql_engine, tmp_path, monkeypatch, point
):
    plan = prepare(mysql_engine, tmp_path)
    before = snapshot(mysql_engine)

    def fail(at):
        if at == point:
            raise RuntimeError("injected failure")

    monkeypatch.setattr(job_state, "fault", fail)
    with pytest.raises(RuntimeError):
        repair_completed_receipt(mysql_engine, Rpc(), **plan)
    assert snapshot(mysql_engine) == before


@requires_mysql
@pytest.mark.parametrize(
    "change", ["backup", "formal_row", "delta", "balance", "progress"]
)
def test_changed_preconditions_abort_without_mutation(mysql_engine, tmp_path, change):
    plan = prepare(mysql_engine, tmp_path)
    if change == "backup":
        plan["backup"]["sha256"] = "0" * 64
    if change == "formal_row":
        plan["expected_rows"][0]["amount_raw"] = "101"
    if change == "delta":
        plan["expected_deltas"][SENDER] = "101"
    if change in ("balance", "progress"):
        with mysql_engine.begin() as c:
            if change == "balance":
                c.execute(
                    balances.update()
                    .where(balances.c.address == SENDER)
                    .values(balance_raw=751)
                )
            else:
                c.execute(jobs.update().values(balances_through=13))
    before = deepcopy(snapshot(mysql_engine))
    with pytest.raises(ChainScopeError):
        repair_completed_receipt(mysql_engine, Rpc(), **plan)
    assert snapshot(mysql_engine) == before
