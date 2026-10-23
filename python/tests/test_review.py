"""Acceptance tests for the 2026-10-15 review; test names carry the item IDs."""

import json
import sys
import threading
import tracemalloc
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
import sqlalchemy as sa
from conftest import requires_mysql
from sqlalchemy.exc import OperationalError
from test_dex import (
    CAROL,
    CHAIN,
    JUNK,
    POOL,
    POOL_INFO,
    WBNB,
    SwapChain,
    block,
    swap_v2,
    transfer,
    tx,
)
from test_jobs import TOKEN, Chain, count, new_job, topic, window_rows
from test_reconcile import Alchemy, TimePortal

import chainscope.__main__ as cli
import chainscope.dex as dex_module
import chainscope.holders as holders_module
import chainscope.jobs as jobs_module
import chainscope.sync as sync_module
from chainscope.config import load_settings
from chainscope.dex import FACTORIES, load_pools, save_pools, set_pool_enabled
from chainscope.errors import ChainScopeError
from chainscope.holders import refresh_balances
from chainscope.jobs import create_job, job_status, load_job, verify_job, window_tally
from chainscope.models import (
    balances,
    batches,
    jobs,
    pools,
    swaps,
    transfers,
    window_diffs,
    window_events,
    windows,
)
from chainscope.query import BEIJING, coverage, parse_time, query_job, summarize
from chainscope.query import swap_summary as query_swaps
from chainscope.rpc_client import RpcClient
from chainscope.sqd import Portal, window_activity
from chainscope.sync import (
    create,
    pools_discover,
    recheck_job,
    redo_job,
    status_with_head,
    sync_job,
)
from chainscope.validator import TRANSFER_TOPIC

BASE = 1_700_000_000
ZERO = "0x" + "0" * 40


def addr(n: int) -> str:
    return "0x" + format(n, "040x")


A, B, C, D = (addr(n) for n in (0xA1, 0xB2, 0xC3, 0xD4))


class RichChain(Chain):
    """Blocks of chosen Transfers; headers and transaction hashes can be changed.

    blocks: {number: [(sender, receiver, amount[, tx_hash]), ...]}; the log
    index is the position in the block.
    """

    def __init__(self, head, blocks):
        super().__init__(head, {})
        self.blocks = blocks
        self.hashes = {}
        self.times = {}

    def block(self, number):
        logs = []
        for index, (sender, receiver, amount, *rest) in enumerate(self.blocks[number]):
            logs.append(
                {
                    "logIndex": index,
                    "transactionHash": rest[0] if rest else tx_of(number, index),
                    "address": TOKEN,
                    "topics": [TRANSFER_TOPIC, topic(sender), topic(receiver)],
                    "data": "0x" + format(amount, "064x"),
                }
            )
        return {
            "header": {
                "number": number,
                "hash": self.hashes.get(number, "0x" + format(number, "064x")),
                "timestamp": self.times.get(number, BASE + number),
            },
            "logs": logs,
        }

    def handler(self, request):
        if request.url.path.endswith("/finalized-head"):
            return httpx.Response(200, json={"number": self.head})
        body = json.loads(request.content)
        self.requests.append((body["fromBlock"], body["toBlock"]))
        content = b"".join(
            json.dumps(self.block(n)).encode() + b"\n"
            for n in sorted(self.blocks)
            if body["fromBlock"] <= n <= body["toBlock"] and self.blocks[n]
        )
        return httpx.Response(200, content=content)


def tx_of(number, index):
    return "0x" + format(number * 1000 + index, "064x")


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


def links(engine):
    return count(engine, window_events)


def diff_kinds(engine):
    with engine.connect() as connection:
        return connection.scalars(
            sa.select(window_diffs.c.kind).order_by(window_diffs.c.diff_id)
        ).all()


def through(engine):
    return job_status(engine, "bem")["verified_through"]


class Crash(BaseException):
    pass


@pytest.fixture
def crash_at(monkeypatch):
    def arm(point):
        def exit_(code):
            raise Crash(code)

        monkeypatch.setenv("CHAINSCOPE_FAULT", point)
        monkeypatch.setattr(jobs_module.os, "_exit", exit_)

    return arm


# P1-01 ------------------------------------------------------------------------


@requires_mysql
def test_p1_01_relink_add_reduce_empty_and_rollback(
    mysql_engine, monkeypatch, crash_at
):
    engine = mysql_engine
    new_job(engine)
    chain = RichChain(14, {11: [(A, B, 5)], 12: [(A, B, 6), (B, C, 7)]})
    run(engine, chain)
    assert links(engine) == 3 and count(engine, transfers) == 3

    # Add: an event the first fetch did not have.
    chain.blocks[13] = [(C, A, 1)]
    redo_job(engine, "bem", first=10, last=14)
    assert run(engine, chain)["windows_done"] == 1
    assert links(engine) == 4 and verify_job(engine, "bem")["ok"]

    # Reduce: the link goes, the raw row stays, the drop is recorded.
    chain.blocks[12] = [(A, B, 6)]
    redo_job(engine, "bem", first=10, last=14)
    run(engine, chain)
    assert links(engine) == 3 and count(engine, transfers) == 4
    assert diff_kinds(engine) == ["dropped_on_redo"]
    assert verify_job(engine, "bem")["ok"]

    # A crash in the middle of a relink leaves the old links untouched.
    chain.blocks = {}
    redo_job(engine, "bem", first=10, last=14)
    crash_at("before_commit")
    with pytest.raises(Crash):
        run(engine, chain)
    engine.dispose()
    monkeypatch.delenv("CHAINSCOPE_FAULT")
    assert links(engine) == 3
    assert window_rows(engine)[0][2] == "running"  # the next run recovers it

    # Empty: every link goes; raw rows other jobs or batches may use remain.
    run(engine, chain)
    assert links(engine) == 0 and count(engine, transfers) == 4
    with engine.connect() as connection:
        assert connection.scalar(sa.select(windows.c.event_count)) == 0
    assert through(engine) == 14 and verify_job(engine, "bem")["ok"]


# P1-02 ------------------------------------------------------------------------


@requires_mysql
def test_p1_02_readback_mismatch_is_rolled_back_and_quarantined(
    mysql_engine, monkeypatch
):
    engine = mysql_engine
    new_job(engine)
    chain = RichChain(14, {11: [(A, B, 5)]})
    real = jobs_module._window_tally

    def wrong(*args):
        tally = real(*args)
        tally.amount += 1  # what MySQL "returned" differs from what was written
        return tally

    monkeypatch.setattr(jobs_module, "_window_tally", wrong)
    summary = run(engine, chain)
    assert [q["error_type"] for q in summary["quarantined"]] == ["ReadbackMismatch"]
    assert window_rows(engine) == [(10, 14, "failed")]
    assert through(engine) is None
    assert count(engine, transfers) == 0 and links(engine) == 0  # rolled back
    assert diff_kinds(engine) == ["readback_mismatch"]
    monkeypatch.setattr(jobs_module, "_window_tally", real)
    assert run(engine, chain)["windows_done"] == 0  # stays quarantined
    redo_job(engine, "bem", first=10, last=14)
    run(engine, chain)
    assert through(engine) == 14


@requires_mysql
def test_p1_02_connection_failure_is_not_a_mismatch(mysql_engine, monkeypatch):
    """Cannot verify now: the window fails as retryable and is not quarantined."""
    engine = mysql_engine
    new_job(engine)
    chain = RichChain(14, {11: [(A, B, 5)]})

    def lost(*args):
        raise OperationalError("SELECT", {}, Exception(2013, "Lost connection"))

    monkeypatch.setattr(jobs_module, "_window_tally", lost)
    with pytest.raises(OperationalError):
        run(engine, chain)
    status = job_status(engine, "bem")
    assert status["failed_windows"][0]["error_type"] == "OperationalError"
    assert status["failed_windows"][0]["quarantined"] is False
    assert status["verified_through"] is None and count(engine, transfers) == 0
    monkeypatch.undo()
    run(engine, chain)  # retried automatically
    assert through(engine) == 14


@requires_mysql
@pytest.mark.parametrize("point", ["before_done", "before_commit", "after_commit"])
def test_p1_02_crash_around_commit_never_leaves_done_without_data(
    mysql_engine, monkeypatch, crash_at, point
):
    engine = mysql_engine
    new_job(engine)
    chain = RichChain(14, {11: [(A, B, 5)], 13: [(B, C, 2)]})
    crash_at(point)
    with pytest.raises(Crash):
        run(engine, chain)
    engine.dispose()
    monkeypatch.delenv("CHAINSCOPE_FAULT")
    committed = point == "after_commit"
    assert window_rows(engine)[0][2] == ("done" if committed else "running")
    assert through(engine) == (14 if committed else None)
    assert links(engine) == (2 if committed else 0)
    run(engine, chain)
    assert through(engine) == 14 and verify_job(engine, "bem")["ok"]


# P1-03 ------------------------------------------------------------------------


def boom(*args, **kwargs):
    raise ChainScopeError("模拟中断")


@requires_mysql
def test_p1_03_discovery_interrupted_at_each_step_never_misses_swaps(
    mysql_engine, monkeypatch
):
    engine = mysql_engine
    new_job(engine)
    chain = SwapChain()
    run(engine, chain)

    # Before save: nothing is written.
    monkeypatch.setattr(dex_module, "save_pools", boom)
    with pytest.raises(ChainScopeError):
        pools_discover(engine, "bem", chain.portal())
    monkeypatch.undo()
    # During save, after inserting pools but before scheduling the backfill:
    # one transaction, so the inserts roll back too.
    monkeypatch.setattr(dex_module, "reset_stale_pool_windows", boom)
    with pytest.raises(ChainScopeError):
        pools_discover(engine, "bem", chain.portal())
    monkeypatch.undo()
    with engine.connect() as connection:
        assert load_pools(connection, "bem", enabled_only=False) == {}
    assert all(status == "done" for *_, status in window_rows(engine))

    # After scheduling: the reset windows are redone by the next sync, and
    # running discover again changes nothing.
    found = pools_discover(engine, "bem", chain.portal())
    assert len(found["added"]) == 2 and found["windows_reset"] == 3
    again = pools_discover(engine, "bem", chain.portal())
    assert again["added"] == [] and again["windows_reset"] == 0
    assert run(engine, chain)["swaps"] == 4
    assert verify_job(engine, "bem")["ok"]


@requires_mysql
def test_p1_03_pool_saved_without_reset_is_repaired_by_sync(mysql_engine):
    """State an older version could leave: pool row present, windows still done."""
    engine = mysql_engine
    new_job(engine)
    chain = SwapChain()
    run(engine, chain)
    with engine.begin() as connection:
        connection.execute(
            pools.insert().values(
                **POOL_INFO,
                job_id="bem",
                token0=TOKEN,
                token1=WBNB,
                quote_symbol="WBNB",
                fee=None,
                created_block=10,
                enabled=True,
                found_by="factory",
                added_at_utc=datetime.now(UTC).replace(tzinfo=None),
            )
        )
    summary = run(engine, chain)
    assert summary["pool_windows_reset"] == 3 and summary["swaps"] == 4


@requires_mysql
def test_p1_03_manual_add_enable_and_disable_are_atomic(mysql_engine, monkeypatch):
    engine = mysql_engine
    new_job(engine)
    chain = SwapChain()
    run(engine, chain)
    row = {
        **POOL_INFO,
        "token0": TOKEN,
        "token1": WBNB,
        "quote_symbol": "WBNB",
        "fee": None,
        "created_block": 10,
    }
    saved = save_pools(engine, "bem", [row], "manual", enable_known=False)
    assert len(saved["added"]) == 1 and saved["windows_reset"] == 0  # disabled
    monkeypatch.setattr(dex_module, "reset_stale_pool_windows", boom)
    with pytest.raises(ChainScopeError):
        set_pool_enabled(engine, "bem", POOL, True)
    monkeypatch.undo()
    with engine.connect() as connection:
        assert load_pools(connection, "bem") == {}  # the flag rolled back too
    assert set_pool_enabled(engine, "bem", POOL, True)["windows_reset"] == 3
    assert run(engine, chain)["swaps"] == 4
    # Disabling removes the swaps and keeps the windows: nothing to refetch.
    disabled = set_pool_enabled(engine, "bem", POOL, False)
    assert disabled["swaps_removed"] == 4 and disabled["windows_reset"] == 0
    assert run(engine, chain)["pool_windows_reset"] == 0
    assert count(engine, swaps) == 0 and verify_job(engine, "bem")["ok"]


# P1-04 ------------------------------------------------------------------------


def busy_blocks(first, last):
    """Deterministic traffic among a few holders, with a mint at the start."""
    people = [addr(0x100 + i) for i in range(6)]
    blocks = {first: [(ZERO, people[0], 10**24)]}
    for n in range(first + 1, last + 1):
        blocks[n] = [
            (people[(n + k) % 6], people[(n * 7 + k) % 6], (n * 37 + k * 11) % 1000 + 1)
            for k in range(3)
        ]
    return blocks


def expected_balances(chain, last):
    result = {}
    for n, items in chain.blocks.items():
        if n > last:
            continue
        for sender, receiver, amount, *_ in items:
            result[sender] = result.get(sender, 0) - amount
            result[receiver] = result.get(receiver, 0) + amount
    result.pop(ZERO, None)
    return {k: v for k, v in result.items() if v}


def stored_balances(engine):
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(balances.c.address, balances.c.balance_raw)
        ).all()
    return {a: int(v) for a, v in rows if int(v)}


def marks(engine):
    with engine.connect() as connection:
        job = load_job(connection, "bem")
    return job["verified_through"], job["balances_through"]


@requires_mysql
def test_p1_04_balances_match_full_recomputation_on_every_path(
    mysql_engine, monkeypatch
):
    engine = mysql_engine
    new_job(engine)
    chain = RichChain(24, busy_blocks(10, 24))
    run(engine, chain)  # first build
    assert stored_balances(engine) == expected_balances(chain, 24)

    chain.head = 34  # incremental
    chain.blocks.update({n: v for n, v in busy_blocks(10, 34).items() if n > 24})
    run(engine, chain)
    assert stored_balances(engine) == expected_balances(chain, 34)

    redo_job(engine, "bem", first=30, last=34)  # short rewind: subtract
    assert marks(engine) == (29, 29)
    assert stored_balances(engine) == expected_balances(chain, 29)

    monkeypatch.setattr(jobs_module, "BALANCE_REWIND_LIMIT", 3)
    redo_job(engine, "bem", first=20, last=24)  # long rewind: clear and rebuild
    assert marks(engine) == (19, None)
    monkeypatch.undo()
    run(engine, chain)
    assert stored_balances(engine) == expected_balances(chain, 34)

    # Deadlock on a chunk: the chunk is retried and applied once.
    redo_job(engine, "bem", first=15, last=34)
    with engine.begin() as connection:  # force a chunked rebuild of 10-14
        connection.execute(jobs.update().values(balances_through=None))
    real = holders_module._refresh_step
    failures = []

    def flaky(*args):
        if len(failures) < 2:
            failures.append(1)
            raise OperationalError("UPDATE", {}, Exception(1213, "Deadlock"))
        return real(*args)

    monkeypatch.setattr(holders_module, "_refresh_step", flaky)
    result = refresh_balances(engine, "bem", chunk=2, sleep=lambda _: None)
    assert result["complete"] and len(failures) == 2
    assert stored_balances(engine) == expected_balances(chain, 14)


@requires_mysql
def test_p1_04_concurrent_refreshes_and_sync_apply_each_range_once(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = RichChain(34, busy_blocks(10, 34))
    run(engine, chain)
    # Forget the balances so two refreshes race on the first build.
    with engine.begin() as connection:
        connection.execute(balances.delete())
        connection.execute(jobs.update().values(balances_through=None))
    gate = threading.Barrier(2)
    errors = []

    def refresh():
        try:
            gate.wait()
            refresh_balances(engine, "bem", chunk=3, sleep=lambda _: None)
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=refresh) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert stored_balances(engine) == expected_balances(chain, 34)

    # A sync that adds windows while a holders refresh keeps running.
    chain.head = 64
    chain.blocks.update({n: v for n, v in busy_blocks(10, 64).items() if n > 34})
    done = threading.Event()

    def keep_refreshing():
        try:
            while not done.is_set():
                refresh_balances(engine, "bem", chunk=2, sleep=lambda _: None)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=keep_refreshing)
    worker.start()
    try:
        run(engine, chain)
    finally:
        done.set()
        worker.join()
    assert errors == []
    refresh_balances(engine, "bem", sleep=lambda _: None)
    assert marks(engine) == (64, 64)
    assert stored_balances(engine) == expected_balances(chain, 64)


# P1-05 ------------------------------------------------------------------------


def at(n):
    return datetime.fromtimestamp(BASE + n, BEIJING).isoformat()


def swap_total(result):
    return sum(item["swaps"] for item in result.get("swaps", {}).get("by_side", []))


@requires_mysql
def test_p1_05_query_counts_transfers_and_swaps_only_up_to_the_watermark(
    mysql_engine,
):
    engine = mysql_engine
    new_job(engine)
    chain = SwapChain()
    pools_discover(engine, "bem", chain.portal())
    run(engine, chain)
    full = query_job(engine, TimePortal(), "bem", at(10), at(25))
    assert full["coverage"]["state"] == "complete"
    assert full["events"] == 8 and swap_total(full) == 4

    # A gap in the middle: 20-24 is still done, but nothing past the gap counts,
    # and the stale rows of the pending window are not read.
    redo_job(engine, "bem", first=15, last=19)
    gap = query_job(engine, TimePortal(), "bem", at(10), at(25))
    assert gap["coverage"]["state"] == "partial"
    assert gap["coverage"]["counted_blocks"] == [10, 14]
    assert gap["events"] == 3 and swap_total(gap) == 1  # block 13 only
    run(engine, chain)

    # A pool backfill pending: its windows are not counted until redone.
    set_pool_enabled(engine, "bem", JUNK, True)
    pending = query_job(engine, TimePortal(), "bem", at(10), at(25))
    assert pending["coverage"]["state"] == "none"
    assert "events" not in pending and "swaps" not in pending
    run(engine, chain)
    again = query_job(engine, TimePortal(), "bem", at(10), at(25))
    assert again["events"] == 8 and swap_total(again) == 4


# P1-06 ------------------------------------------------------------------------


@requires_mysql
@pytest.mark.parametrize(
    ("change", "field"),
    [
        ("hash", "block_hash"),
        ("time", "block_time_utc"),
        ("block", "block_number"),
    ],
)
def test_p1_06_recheck_detects_block_hash_and_time_changes(mysql_engine, change, field):
    engine = mysql_engine
    new_job(engine)
    chain = RichChain(14, {11: [(A, B, 5)], 12: [(B, C, 1)]})
    run(engine, chain)
    if change == "hash":
        chain.hashes[11] = "0x" + "f" * 64
    elif change == "time":
        chain.times[11] = BASE + 11 + 1
    else:  # the same transaction and log index now reported in block 13
        chain.blocks[13] = [(A, B, 5, tx_of(11, 0))]
        del chain.blocks[11]
    result = recheck_job(engine, "bem", chain.portal(), sample=5, notify=lambda _: None)
    assert result["changed_windows"] == [{"window": [10, 14], "diffs": 1}]
    with engine.connect() as connection:
        diff = connection.execute(sa.select(window_diffs)).mappings().one()
    assert diff["kind"] == "recheck_transfer_changed"
    assert field in diff["detail"]["fields"]
    status = job_status(engine, "bem")
    assert status["verified_through"] is None
    assert status["failed_windows"][0]["error_type"] == "RecheckChanged"
    assert status["failed_windows"][0]["quarantined"]
    # Even after a redo the stored row is not overwritten: a finalized block
    # that changed is evidence to investigate, so the window stays quarantined.
    redo_job(engine, "bem", first=10, last=14)
    summary = run(engine, chain)
    assert [q["error_type"] for q in summary["quarantined"]] == ["EventConflict"]


@requires_mysql
def test_p1_06_verify_job_checks_headers_inside_mysql_and_old_fingerprints(
    mysql_engine,
):
    engine = mysql_engine
    new_job(engine)
    run(engine, RichChain(14, {11: [(A, B, 5), (B, C, 6)]}))
    # Windows stored before version 2 keep a v1 fingerprint and still verify.
    old = window_tally(engine, "bem", 10, version=1).fingerprint_bytes()
    with engine.begin() as connection:
        connection.execute(
            windows.update().values(fingerprint=old, fingerprint_version=None)
        )
    checked = verify_job(engine, "bem")
    assert checked["ok"] and checked["fingerprint_versions"] == [1]
    # Two rows of one block with different hashes: an internal contradiction.
    with engine.begin() as connection:
        connection.execute(
            transfers.update()
            .where(transfers.c.log_index == 1)
            .values(block_hash="0x" + "e" * 64)
        )
    checked = verify_job(engine, "bem")
    assert checked["blocks_with_conflicting_header"] == [11] and not checked["ok"]


# P1-07 ------------------------------------------------------------------------


@requires_mysql
@pytest.mark.parametrize(
    ("number", "replacement", "kind", "fields"),
    [
        (  # a swap appears that was not there
            19,
            block(
                19,
                [
                    (
                        CAROL,
                        [
                            transfer(POOL, CAROL, 7, tx(19)),
                            swap_v2(POOL, 0, 1, 7, 0, tx(19)),
                        ],
                    )
                ],
            ),
            "recheck_swap_new_in_source",
            None,
        ),
        (  # a swap disappears
            13,
            block(
                13, [("0x" + "5" * 40, [transfer(POOL, "0x" + "5" * 40, 100, tx(13))])]
            ),
            "recheck_swap_missing_in_source",
            None,
        ),
        (  # the quote amount changed
            13,
            block(
                13,
                [
                    (
                        "0x" + "5" * 40,
                        [
                            transfer(POOL, "0x" + "5" * 40, 100, tx(13)),
                            swap_v2(POOL, 0, 6, 100, 0, tx(13)),
                        ],
                    )
                ],
            ),
            "recheck_swap_changed",
            {"quote_amount_raw"},
        ),
        (  # another sender: the trader and its attribution change
            13,
            block(
                13,
                [
                    (
                        CAROL,
                        [
                            transfer(POOL, "0x" + "5" * 40, 100, tx(13)),
                            swap_v2(POOL, 0, 5, 100, 0, tx(13)),
                        ],
                    )
                ],
            ),
            "recheck_swap_changed",
            {"tx_from", "trader", "attribution"},
        ),
    ],
)
def test_p1_07_recheck_compares_swaps(
    mysql_engine, monkeypatch, number, replacement, kind, fields
):
    engine = mysql_engine
    new_job(engine)
    chain = SwapChain()
    pools_discover(engine, "bem", chain.portal())
    run(engine, chain)
    monkeypatch.setitem(CHAIN, number, replacement)
    result = recheck_job(engine, "bem", chain.portal(), sample=5, notify=lambda _: None)
    assert result["checked"]["swaps"].startswith("3 个窗口逐字段")
    assert len(result["changed_windows"]) == 1
    with engine.connect() as connection:
        found = connection.execute(
            sa.select(window_diffs.c.kind, window_diffs.c.detail)
        ).all()
    swap_diffs = [d for k, d in found if k.startswith("recheck_swap")]
    assert [k for k, _ in found if k.startswith("recheck_swap")] == [kind]
    if fields:
        assert fields <= set(swap_diffs[0]["fields"])


@requires_mysql
def test_p1_07_recheck_says_when_swaps_were_not_compared(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = SwapChain()
    run(engine, chain)
    with engine.begin() as connection:  # a pool the windows were not collected with
        connection.execute(
            pools.insert().values(
                **POOL_INFO,
                job_id="bem",
                token0=TOKEN,
                token1=WBNB,
                quote_symbol="WBNB",
                fee=None,
                created_block=10,
                enabled=True,
                found_by="manual",
                added_at_utc=datetime.now(UTC).replace(tzinfo=None),
            )
        )
    result = recheck_job(engine, "bem", chain.portal(), sample=5, notify=lambda _: None)
    assert result["checked"]["swaps"].startswith(
        "0 个窗口逐字段；3 个窗口池子集合已变化"
    )


# P2-01 ------------------------------------------------------------------------


@requires_mysql
def test_p2_01_windows_record_the_parser_version_that_ran(mysql_engine, monkeypatch):
    engine = mysql_engine
    new_job(engine)  # the job row says version 1
    monkeypatch.setattr(sync_module, "PARSER_VERSION", 3)
    chain = RichChain(24, {11: [(A, B, 1)]})
    assert run(engine, chain)["parser_version"] == 3
    with engine.connect() as connection:
        versions = connection.scalars(sa.select(windows.c.parser_version)).all()
    assert versions == [3, 3, 3]
    assert redo_job(engine, "bem")["windows_reset"] == 0  # upgraded: not reselected
    with engine.begin() as connection:
        connection.execute(
            windows.update().where(windows.c.start_block == 15).values(parser_version=2)
        )
    assert redo_job(engine, "bem")["windows_reset"] == 1
    run(engine, chain)
    assert redo_job(engine, "bem")["windows_reset"] == 0


# P2-02 ------------------------------------------------------------------------


class DeadPortalChain(Chain):
    def handler(self, request):
        return httpx.Response(400)


@requires_mysql
def test_p2_02_status_uses_the_live_head_and_cli_exit_codes(
    mysql_engine, monkeypatch, capsys
):
    engine = mysql_engine
    new_job(engine)
    chain = Chain(24, {})
    run(engine, chain)
    live = status_with_head(engine, "bem", chain.portal(), max_lag_blocks=5)
    assert live["current_head"] == 24 and live["head_observed_at_utc"] is not None
    assert live["behind"] is False

    settings = replace(load_settings(), mysql_database=engine.url.database)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(
        sys, "argv", ["cs", "status", "--job", "bem", "--max-lag-blocks", "5"]
    )
    current = {"chain": chain}
    monkeypatch.setattr(cli, "portal_for", lambda *a, **k: current["chain"].portal())

    def status_code():
        code = cli.main()
        return code, json.loads(capsys.readouterr().out)

    assert status_code()[0] == 0
    chain.head = 40  # the chain moved on since the last run
    code, shown = status_code()
    assert code == 2 and shown["lag_blocks"] == 16
    current["chain"] = DeadPortalChain(0, {})
    code, shown = status_code()
    assert code == 4 and shown["behind"] == "unknown" and shown["lag_blocks"] is None
    assert shown["current_head"] is None and "HTTP 400" in shown["head_error"]


# P2-03 ------------------------------------------------------------------------


@requires_mysql
def test_p2_03_query_separates_before_deploy_not_collected_and_verified(
    mysql_engine,
):
    engine = mysql_engine
    create_job(
        engine,
        "bem",
        TOKEN,
        source="sqd",
        start_block=10,
        window_size=5,
        parser_version=1,
        decimals=18,
        deploy_block=5,
    )
    run(engine, RichChain(14, {11: [(A, B, 5)]}))
    before = query_job(engine, TimePortal(), "bem", at(0), at(5))
    assert before["coverage"]["state"] == "complete" and before["events"] == 0
    assert [p["kind"] for p in before["coverage"]["parts"]] == ["before_deploy"]
    skipped = query_job(engine, TimePortal(), "bem", at(5), at(10))
    assert skipped["coverage"]["state"] == "none" and "events" not in skipped
    mixed = query_job(engine, TimePortal(), "bem", at(3), at(15))
    assert [(p["kind"], p["blocks"]) for p in mixed["coverage"]["parts"]] == [
        ("before_deploy", [3, 4]),
        ("not_collected", [5, 9]),
        ("verified", [10, 14]),
    ]
    assert mixed["coverage"]["state"] == "partial" and mixed["events"] == 1


def test_p2_03_without_a_deploy_block_nothing_is_claimed_empty():
    legacy = {"start_block": 10, "verified_through": 20, "deploy_block": None}
    result = coverage(legacy, 0, 9)
    assert result["state"] == "none" and result["deploy_block_unknown"]
    ended = {**legacy, "deploy_block": 10, "end_block": 20}
    assert [p["kind"] for p in coverage(ended, 15, 30)["parts"]] == [
        "verified",
        "outside_job",
    ]


# P2-04 ------------------------------------------------------------------------


def test_p2_04_fractional_seconds_are_rejected_and_offsets_agree():
    with pytest.raises(ChainScopeError, match="小数秒"):
        parse_time("2026-10-01T08:00:00.5")
    assert parse_time("2026-10-01T08:00") == parse_time("2026-10-01T00:00:00+00:00")
    assert parse_time("2026-10-01") == parse_time("2026-09-30T16:00:00Z")


class SharedSecondPortal:
    """Blocks 10, 11 and 12 share one timestamp, as sub-second BSC blocks can."""

    stamps = {n: BASE + n for n in range(0, 10)} | {10: BASE + 10, 11: BASE + 10}
    stamps |= {12: BASE + 10} | {n: BASE + n - 2 for n in range(13, 40)}

    def block_at(self, timestamp):
        return min(n for n, t in self.stamps.items() if t >= timestamp)


@requires_mysql
def test_p2_04_blocks_sharing_a_second_are_all_in_or_all_out(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    run(engine, RichChain(14, {10: [(A, B, 1)], 11: [(A, B, 2)], 12: [(A, B, 3)]}))
    portal = SharedSecondPortal()
    second = query_job(engine, portal, "bem", at(10), at(11))
    assert second["range"]["blocks"] == [10, 12] and second["events"] == 3
    before = query_job(engine, portal, "bem", at(9), at(10))
    assert before["range"]["blocks"] == [9, 9]
    utc = datetime.fromtimestamp(BASE + 10, UTC).isoformat()
    assert query_job(engine, portal, "bem", utc, at(11))["events"] == 3


# P2-05 ------------------------------------------------------------------------


@requires_mysql
def test_p2_05_amounts_past_65_digits_sum_exactly_and_balances_refuse(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    widest = 2**256 - 1  # 78 digits
    edge = 10**65 - 1  # the largest 65-digit value
    over = 10**65  # 66 digits
    chain = RichChain(14, {11: [(A, B, widest), (A, B, widest)], 12: [(B, C, edge)]})
    chain.blocks[13] = [(C, D, over)]
    summary = run(engine, chain)
    # Collection is unaffected; only the DECIMAL(65,0) balance cache refuses.
    assert summary["windows_done"] == 1
    assert "DECIMAL(65,0)" in summary["balances"]["error"]
    with engine.connect() as connection:
        assert connection.scalar(sa.select(jobs.c.balances_through)) is None
        assert connection.scalar(sa.select(sa.func.count()).select_from(balances)) == 0
    result = query_job(engine, TimePortal(), "bem", at(10), at(15))
    assert result["amount_raw_sum"] == str(2 * widest + edge + over)


@requires_mysql
def test_p2_05_balances_exact_just_under_the_decimal_limit(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    big = 10**64 - 1
    chain = RichChain(14, {11: [(ZERO, A, big)], 12: [(A, B, big)]})
    run(engine, chain)
    assert stored_balances(engine) == {B: big}


@requires_mysql
def test_p2_05_swap_sums_are_exact_python_integers(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    huge = 2**256 - 1
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as connection:
        for i in range(3):
            connection.execute(
                swaps.insert().values(
                    job_id="bem",
                    chain_id=56,
                    pool_address=POOL,
                    block_number=11,
                    block_time_utc=now,
                    tx_hash=tx(500 + i),
                    log_index=0,
                    tx_from=A,
                    trader=A,
                    side="buy",
                    token_amount_raw=str(huge),
                    quote_amount_raw=str(10**70),
                    quote_token=WBNB,
                    attribution="direct",
                    transfer_matched=True,
                )
            )
        job = load_job(connection, "bem")
    side = query_swaps(engine, job, 10, 14)["by_side"][0]
    assert side["token_amount_raw"] == str(3 * huge)
    assert side["quote_amount_raw"] == str(3 * 10**70)


# P2-06 ------------------------------------------------------------------------


@requires_mysql
def test_p2_06_one_block_over_the_limit_is_kept_whole_up_to_the_hard_limit(
    mysql_engine,
):
    engine = mysql_engine
    new_job(engine)
    chain = RichChain(14, {12: [(A, B, i + 1) for i in range(5)]})
    summary = run(engine, chain, event_limit=2, block_hard_limit=10)
    assert summary["splits"] >= 2
    rows = window_rows(engine)
    assert (12, 12, "done") in rows and through(engine) == 14
    assert count(engine, transfers) == 5


@requires_mysql
def test_p2_06_one_block_over_the_hard_limit_is_quarantined(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = RichChain(14, {12: [(A, B, i + 1) for i in range(5)]})
    summary = run(engine, chain, event_limit=2, block_hard_limit=4)
    assert [(q["window"], q["error_type"]) for q in summary["quarantined"]] == [
        ([12, 12], "BlockTooLarge")
    ]
    assert through(engine) == 11 and count(engine, transfers) == 0


@requires_mysql
def test_p2_06_swaps_count_toward_the_event_limit(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = SwapChain()
    pools_discover(engine, "bem", chain.portal())
    # Window 15-19 holds 3 Transfers and 2 swaps: 5 items over a limit of 4.
    summary = run(engine, chain, event_limit=4)
    assert summary["splits"] >= 1 and summary["swaps"] == 4


class FixedPortal(Portal):
    def __init__(self, content):
        super().__init__(
            "https://portal.invalid",
            "binance-mainnet",
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=content)
                )
            ),
            sleep=lambda _: None,
            min_interval=0,
        )


def single_block_peak(events):
    chain = RichChain(12, {12: [(A, B, i + 1) for i in range(events)]})
    content = json.dumps(chain.block(12)).encode() + b"\n"
    portal = FixedPortal(content)
    tracemalloc.start()
    try:
        found, _ = window_activity(portal, TOKEN, 12, 12, 10, None, 10**6)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert len(found) == events
    return peak, len(content)


def test_p2_06_single_block_memory_peak_is_bounded():
    peak, size = single_block_peak(20_000)
    # Measured about 6.5x the response size (parsed JSON plus event rows):
    # 20,000 Transfers is about 9 MiB of JSON and 58 MiB of Python memory.
    assert peak < 10 * size
    assert peak < 96 * 2**20


def test_p2_06_block_line_over_the_byte_limit_splits_then_quarantines(monkeypatch):
    import chainscope.sqd as sqd_module

    monkeypatch.setattr(sqd_module, "MAX_LINE_BYTES", 2000)
    chain = RichChain(12, {12: [(A, B, i + 1) for i in range(20)]})
    portal = FixedPortal(json.dumps(chain.block(12)).encode() + b"\n")
    with pytest.raises(sqd_module.TooManyEvents) as raised:
        window_activity(portal, TOKEN, 10, 14, 1000)
    assert raised.value.held is None and raised.value.block is None
    with pytest.raises(sqd_module.BlockTooLarge):
        window_activity(portal, TOKEN, 12, 12, 1000)


# P2-07 ------------------------------------------------------------------------


def seed_distinct(engine, n):
    """n Transfers, each between two addresses no other row uses."""
    now = datetime(2023, 11, 14, 22, 13, 20)
    rows = [
        {
            "chain_id": 56,
            "token_address": TOKEN,
            "block_number": 11,
            "block_hash": "0x" + format(11, "064x"),
            "block_time_utc": now,
            "tx_hash": "0x" + format(i, "064x"),
            "log_index": 0,
            "from_address": addr(2 * i + 1),
            "to_address": addr(2 * i + 2),
            "amount_raw": str(i + 1),
            "token_decimals": 18,
        }
        for i in range(n)
    ]
    with engine.begin() as connection:
        for offset in range(0, n, 5000):
            connection.execute(transfers.insert(), rows[offset : offset + 5000])
        connection.execute(
            sa.text(
                "INSERT INTO window_events (job_id, start_block, event_id) "
                "SELECT 'bem', 10, event_id FROM transfer_events"
            )
        )


@requires_mysql
def test_p2_07_query_memory_does_not_grow_with_distinct_addresses(mysql_engine):
    engine = mysql_engine
    peaks = {}
    for n in (2_000, 20_000):
        with engine.begin() as connection:
            connection.execute(window_events.delete())
            connection.execute(windows.delete())
            connection.execute(batches.delete())
            connection.execute(transfers.delete())
            connection.execute(jobs.delete())
        new_job(engine)
        run(engine, Chain(14, {}))  # one done window 10-14, watermark 14
        seed_distinct(engine, n)
        with engine.connect() as connection:
            job = load_job(connection, "bem")
        tracemalloc.start()
        try:
            result = summarize(engine, job, 10, 14)
            peaks[n] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert result["unique_senders"] == n and result["events"] == n
    # Ten times the addresses must not mean ten times the memory.
    assert peaks[20_000] < 2 * peaks[2_000] + 2**20


# P2-08 ------------------------------------------------------------------------


def measure_tools():
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1] / "tools"))
    import measure_sources

    return measure_sources


def day_start(text):
    return int(datetime.fromisoformat(text + "T00:00:00+08:00").timestamp())


def counts(days):
    tally = measure_tools().Tally()
    for day, n in days.items():
        tally.days[day] += n
        tally.events += n
    return tally


def test_p2_08_daily_average_includes_zero_days_and_skips_partial_ones():
    density = measure_tools().density
    # 10:00 on the 1st to 12:00 on the 12th: the 1st and 12th are partial.
    start = day_start("2026-10-01") + 10 * 3600
    end = day_start("2026-10-12") + 12 * 3600
    sparse = density(counts({"2026-10-01": 50, "2026-10-05": 70}), start, end)
    assert sparse["calendar_days"] == 12 and sparse["full_days"] == 10
    assert sparse["partial_days"] == ["2026-10-01", "2026-10-12"]
    assert sparse["full_day_average"] == 7.0  # 70 over 10 full days
    assert sparse["last_7_full_days"] == [f"2026-10-{d:02d}" for d in range(5, 12)]
    assert sparse["last_7_full_days_average"] == 10.0
    assert sparse["zero_event_full_days"] == 9

    empty = density(counts({}), start, end)
    assert empty["full_day_average"] == 0.0 and empty["busiest_day"] is None

    short = density(counts({"2026-10-02": 9}), day_start("2026-10-01"), end - 86_400)
    assert short["full_days"] == 10 and short["last_7_full_days_average"] == 0.0

    few = density(counts({"2026-10-02": 9}), start, day_start("2026-10-04") + 5)
    assert few["full_days"] == 2 and few["last_7_full_days_average"] == 4.5
    assert "只有 2 天" in few["note"]
    none = density(counts({}), start, start + 3600)
    assert none["full_day_average"] is None and "没有完整自然日" in none["note"]


# P2-09 ------------------------------------------------------------------------


def chunks(consumed, size=100, total=10_000):
    for _ in range(total):
        consumed.append(1)
        yield b"x" * size


@pytest.mark.parametrize("declared", [None, "10"])
def test_p2_09_rpc_limit_is_enforced_while_streaming(monkeypatch, declared):
    import chainscope.rpc_client as rpc_module

    monkeypatch.setattr(rpc_module, "MAX_RESPONSE_BYTES", 1000)
    consumed = []

    def handler(request):
        headers = {"Content-Length": declared} if declared else {}
        return httpx.Response(200, headers=headers, content=chunks(consumed))

    rpc = RpcClient(
        "https://example.invalid/key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )
    with pytest.raises(ChainScopeError, match="16 MiB"):
        rpc.call("eth_chainId", [])
    assert len(consumed) <= 11  # stopped right after passing the limit


def test_p2_09_portal_lines_and_small_responses_are_bounded(monkeypatch):
    import chainscope.sqd as sqd_module

    monkeypatch.setattr(sqd_module, "MAX_LINE_BYTES", 1000)
    monkeypatch.setattr(sqd_module, "MAX_GET_BYTES", 1000)
    consumed = []

    def handler(request):
        # One endless line, sent chunked with no Content-Length.
        return httpx.Response(200, content=chunks(consumed))

    portal = Portal(
        "https://portal.invalid",
        "binance-mainnet",
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
        min_interval=0,
    )
    with pytest.raises(ChainScopeError):
        list(portal.stream({}, 1, 2))
    assert len(consumed) <= 11
    consumed.clear()
    with pytest.raises(ChainScopeError, match="1 MiB"):
        portal.finalized_head()
    assert len(consumed) <= 11


def test_p2_09_market_responses_are_bounded(monkeypatch):
    import chainscope.market as market_module

    monkeypatch.setattr(market_module, "MAX_BYTES", 1000)
    consumed = []
    http = market_module.Http(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=chunks(consumed))
            )
        ),
        sleep=lambda _: None,
    )
    with pytest.raises(ChainScopeError, match="8 MiB"):
        http.get("https://api.example.invalid/x")
    assert len(consumed) <= 11


# P2-10 ------------------------------------------------------------------------


@requires_mysql
def test_p2_10_time_budget_is_soft_and_the_overrun_is_reported(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    now = [0.0]

    def clock():
        now[0] += 4.0  # every look at the clock costs four "seconds"
        return now[0]

    summary = run(engine, Chain(24, {}), max_seconds=5, clock=clock)
    assert summary["stopped"] == "max_runtime" and summary["windows_done"] >= 1
    assert summary["budget_overrun_seconds"] > 0
    assert summary["budget_overrun_seconds"] == round(summary["elapsed_seconds"] - 5, 1)


def test_p2_10_requests_count_attempts_and_compute_units_count_answers():
    replies = [httpx.Response(429, headers={"Retry-After": "1"})]

    def handler(request):
        body = json.loads(request.content)
        if replies:
            return replies.pop()
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": "0x38"}
        )

    rpc = RpcClient(
        "https://example.invalid/key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )
    rpc.call("eth_chainId", [])
    rpc.call("eth_blockNumber", [])
    assert rpc.requests == 3  # every HTTP attempt, retries included
    assert dict(rpc.calls) == {"eth_chainId": 1, "eth_blockNumber": 1}
    assert rpc.compute_units() == 0 + 10  # only answered calls are billed


# P2-11 ------------------------------------------------------------------------


class CreateRpc:
    """Chain id, head 100, code from block 5, decimals 18, block n at BASE + n."""

    def handler(self, request):
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if method == "eth_chainId":
            result = "0x38"
        elif method == "eth_blockNumber":
            result = hex(100)
        elif method == "eth_getCode":
            result = "0x6080" if int(params[1], 16) >= 5 else "0x"
        elif method == "eth_call":
            result = "0x" + format(18, "064x")
        elif method == "eth_getBlockByNumber":
            result = {"timestamp": hex(BASE + int(params[0], 16))}
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


@requires_mysql
def test_p2_11_job_by_time_converts_once_and_resumes(mysql_engine):
    engine = mysql_engine
    settings = replace(load_settings(), mysql_database=engine.url.database)

    def make(job_id, token=TOKEN, **times):
        return create(
            settings,
            job_id,
            token,
            window_size=5,
            portal=TimePortal(),
            rpc=CreateRpc().client(),
            **times,
        )

    made = make("bem", from_time=at(20), to_time=at(30))
    assert (made["start_block"], made["end_block"]) == (20, 29)
    assert made["deploy_block"] == 5 and made["follows_head"] is False
    assert made["start_block_time_beijing"] == at(20)
    assert made["end_block_time_beijing"] == at(29)
    assert made["range_rule"].startswith("[from, to)")
    early = make("early", token=addr(0xE1), from_time=at(1))
    assert early["start_block"] == 5 and "部署区块" in early["notes"][0]
    with pytest.raises(ChainScopeError, match="晚于现在"):
        make("late", token=addr(0xE2), from_time="2999-01-01")
    with pytest.raises(ChainScopeError, match="晚于现在"):
        make("late", token=addr(0xE2), to_time="2999-01-01")
    with pytest.raises(ChainScopeError, match="已存在"):
        make("bem", from_time=at(20))
    # New jobs name the Alchemy primary; the legacy SQD runner refuses them.
    assert made["source"] == "alchemy"
    chain = RichChain(100, {21: [(A, B, 1)], 31: [(A, B, 2)]})
    with pytest.raises(ChainScopeError, match="旧 SQD 主采路径"):
        run(engine, chain)
    with engine.begin() as connection:
        connection.execute(
            jobs.update().where(jobs.c.job_id == "bem").values(source="sqd")
        )
    # The job keeps block numbers: syncing twice continues, never restarts.
    first = run(engine, chain)
    assert first["windows_done"] == 2 and through(engine) == 29
    assert run(engine, chain)["windows_done"] == 0
    status = job_status(engine, "bem", max_lag_blocks=0, head=100)
    assert status["lag_blocks"] == 0 and status["behind"] is False
    assert count(engine, transfers) == 1  # block 31 is outside the job


# P2-12 ------------------------------------------------------------------------


@requires_mysql
def test_p2_12_finality_needs_both_sources_or_says_sqd_only(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = Chain(24, {11: 1})
    silent = Alchemy(chain, finalized=None)  # RPC without a finalized tag
    with silent.client() as rpc:
        with pytest.raises(ChainScopeError, match="finalized"):
            run(engine, chain, rpc)
    assert window_rows(engine) == []  # nothing planned or collected
    alone = run(engine, chain)
    assert alone["finality"] == "sqd_only" and alone["reconcile"] == "skipped"
    chain.head = 30
    with Alchemy(chain, finalized=27).client() as rpc:
        both = run(engine, chain, rpc)
    assert both["finality"] == "min(sqd, rpc)" and both["source_head"] == 27


@requires_mysql
def test_sync_remembers_quarantine_across_runs_and_cli_exits_nonzero(
    mysql_engine, monkeypatch, capsys
):
    engine = mysql_engine
    new_job(engine)
    chain = RichChain(24, {11: [(A, B, 5)]})
    assert run(engine, chain)["coverage_complete"]
    redo_job(engine, "bem", first=10, last=14)
    chain.blocks[11] = [(A, B, 6)]
    first = run(engine, chain)
    assert first["stopped"] == "quarantined"
    assert first["quarantined_remaining"] == 1

    settings = replace(load_settings(), mysql_database=engine.url.database)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "run_sync", lambda *a, **k: run(engine, chain))
    monkeypatch.setattr(sys, "argv", ["cs", "sync", "--job", "bem"])
    # No new mismatch this run, but the durable quarantine still blocks success.
    assert cli.main() == 3
    retry = json.loads(capsys.readouterr().out)
    assert retry["quarantined"] == []
    assert retry["quarantined_remaining"] == 1
    assert retry["stopped"] == "quarantined"
    assert retry["verified_through"] is None
    assert retry["target_block"] == 24 and not retry["coverage_complete"]
    assert job_status(engine, "bem")["quarantined_windows"] == 1

    # Explicitly release the window after restoring the investigated source.
    chain.blocks[11] = [(A, B, 5)]
    redo_job(engine, "bem", first=10, last=14)
    assert cli.main() == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["stopped"] == "caught_up"
    assert recovered["quarantined_remaining"] == 0
    assert recovered["verified_through"] == 24 and recovered["coverage_complete"]


@requires_mysql
def test_sync_quarantine_count_is_not_limited_to_ten_examples(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = Chain(74, {})
    run(engine, chain)
    with engine.begin() as connection:
        connection.execute(
            windows.update().values(status="failed", error_type="EventConflict")
        )
        connection.execute(jobs.update().values(verified_through=None))
    result = run(engine, chain, max_seconds=0)
    assert result["stopped"] == "max_runtime"
    assert result["quarantined_remaining"] == 13
    assert not result["coverage_complete"]
    status = job_status(engine, "bem")
    assert len(status["failed_windows"]) == 10
    assert status["quarantined_windows"] == 13


def test_measurement_tool_retries_529_for_time_lookup():
    attempts, waits = [], []

    def handler(request):
        attempts.append(request.url.path)
        if len(attempts) == 1:
            return httpx.Response(529, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"block_number": 123})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = measure_tools().Portal(
            "https://portal.invalid",
            "binance-mainnet",
            http,
            sleep=waits.append,
            min_interval=0,
        )
        assert client.dataset_get("timestamps/1791907200/block") == {
            "block_number": 123
        }
    assert attempts == [attempts[0]] * 2
    assert waits == [2]


# Follow-ups from the first real run -------------------------------------------


@requires_mysql
def test_split_log_says_what_triggered_it(mysql_engine, tmp_path):
    engine = mysql_engine
    new_job(engine)
    chain = RichChain(14, {12: [(A, B, i + 1) for i in range(5)]})
    path = tmp_path / "sync.jsonl"
    run(engine, chain, event_limit=2, block_hard_limit=10, log_path=path)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    first = next(line for line in lines if "split" in line)
    assert first["split"] == [10, 14]
    assert first["reason"] == "event_limit" and first["event_limit"] == 2
    # Block 12 alone holds 5 Transfers: the count passes 2 while reading it.
    assert first["held"] == 5 and first["at_block"] == 12


class FailingFactoryChain(SwapChain):
    """Factory scans from `fail_from` on are refused, like a Portal outage."""

    def __init__(self, fail_from):
        super().__init__()
        self.fail_from = fail_from

    def handler(self, request):
        if request.url.path.endswith("/finalized-stream"):
            body = json.loads(request.content)
            factory = any(
                FACTORIES["pancake_v2"][0] in f["address"] for f in body["logs"]
            )
            if factory and body["fromBlock"] >= self.fail_from:
                return httpx.Response(400, json={"error": "refused"})
        return super().handler(request)


@requires_mysql
def test_pools_discover_scans_in_chunks_and_resumes(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    messages = []
    chain = FailingFactoryChain(fail_from=16)
    with pytest.raises(ChainScopeError, match="--from-block 16"):
        pools_discover(
            engine, "bem", chain.portal(), chunk_blocks=3, notify=messages.append
        )
    # Chunks 10-12 and 13-15 finished: the pools created at block 10 are kept.
    with engine.connect() as connection:
        assert len(load_pools(connection, "bem", enabled_only=False)) == 2
    assert len(messages) == 2

    chain.fail_from = 10**9
    rest = pools_discover(
        engine, "bem", chain.portal(), from_block=16, chunk_blocks=3, notify=None
    )
    assert rest["scanned"] == [16, 24] and rest["added"] == []
    # One scan of the whole range finds the same pools as the chunked one.
    whole = pools_discover(engine, "bem", SwapChain().portal(), notify=None)
    assert whole["scanned"] == [10, 24] and whole["added"] == []


@requires_mysql
def test_pools_discover_rejects_an_empty_range(mysql_engine):
    new_job(mysql_engine)
    with pytest.raises(ChainScopeError, match="扫描区间无效"):
        pools_discover(
            mysql_engine, "bem", SwapChain().portal(), from_block=30, notify=None
        )


def test_portal_sends_the_key_only_when_configured():
    seen = []

    def handler(request):
        seen.append(request.headers.get("x-api-key"))
        if request.url.path.endswith("/finalized-head"):
            return httpx.Response(200, json={"number": 5})
        return httpx.Response(200, content=b"")

    for key in ("k-123", ""):
        portal = Portal(
            "https://portal.invalid",
            "binance-mainnet",
            httpx.Client(transport=httpx.MockTransport(handler)),
            api_key=key,
            sleep=lambda _: None,
            min_interval=0,
        )
        portal.finalized_head()
        list(portal.stream({"type": "evm"}, 1, 5))
    assert seen == ["k-123", "k-123", None, None]


def test_portal_address_and_key_come_from_settings(monkeypatch):
    monkeypatch.setenv("SQD_API_KEY", "k-456")
    monkeypatch.setenv("SQD_PORTAL_URL", "https://portal.example/")
    settings = load_settings()
    assert "k-456" not in repr(settings)
    portal = sync_module.portal_for(settings, httpx.Client())
    assert portal._base == "https://portal.example"
    assert portal._headers == {"x-api-key": "k-456"}
