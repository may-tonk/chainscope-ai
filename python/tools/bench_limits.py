"""Measure memory and time of the two in-memory hot spots, at several scales.

1. One block parsed from the Portal stream (no network: a synthetic response).
2. `query` totals over N Transfers with 2N distinct addresses, in MySQL.

Part 2 creates its own throwaway database (chainscope_bench_<random>) on the
configured MySQL server and drops it afterwards; the configured database is
never read or written. It needs --mysql to run.

    python tools/bench_limits.py --blocks 1000 20000 50000
    python tools/bench_limits.py --mysql --rows 10000 100000 1000000
"""

import argparse
import json
import time
import tracemalloc
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

import httpx
import sqlalchemy as sa

from chainscope.config import load_settings
from chainscope.database import engine_for, initialize
from chainscope.jobs import create_job, load_job
from chainscope.models import jobs, transfers, window_events, windows
from chainscope.query import summarize
from chainscope.sqd import Portal, window_activity
from chainscope.validator import TRANSFER_TOPIC

TOKEN = "0x" + "1" * 40


def address(n: int) -> str:
    return "0x" + format(n, "040x")


def topic(value: str) -> str:
    return "0x" + "0" * 24 + value[2:]


def block_line(number: int, events: int) -> bytes:
    logs = [
        {
            "logIndex": i,
            "transactionHash": "0x" + format(number * 10**6 + i, "064x"),
            "address": TOKEN,
            "topics": [TRANSFER_TOPIC, topic(address(i + 1)), topic(address(i + 2))],
            "data": "0x" + format(i + 1, "064x"),
        }
        for i in range(events)
    ]
    header = {"number": number, "hash": "0x" + format(number, "064x"), "timestamp": 1}
    return json.dumps({"header": header, "logs": logs}).encode() + b"\n"


def one_block(events: int) -> dict:
    content = block_line(12, events)
    portal = Portal(
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
    tracemalloc.start()
    began = time.perf_counter()
    try:
        found, _ = window_activity(portal, TOKEN, 12, 12, 1, None, 10**7)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return {
        "events": len(found),
        "response_mib": round(len(content) / 2**20, 2),
        "python_peak_mib": round(peak / 2**20, 1),
        "seconds": round(time.perf_counter() - began, 2),
    }


def seed(engine, rows: int) -> None:
    moment = datetime(2023, 11, 14, 22, 13, 20)
    with engine.begin() as connection:
        for offset in range(0, rows, 10_000):
            connection.execute(
                transfers.insert(),
                [
                    {
                        "chain_id": 56,
                        "token_address": TOKEN,
                        "block_number": 11,
                        "block_hash": "0x" + format(11, "064x"),
                        "block_time_utc": moment,
                        "tx_hash": "0x" + format(i, "064x"),
                        "log_index": 0,
                        "from_address": address(2 * i + 1),
                        "to_address": address(2 * i + 2),
                        "amount_raw": str(10**18 + i),
                        "token_decimals": 18,
                    }
                    for i in range(offset, min(rows, offset + 10_000))
                ],
            )
        connection.execute(
            sa.text(
                "INSERT INTO window_events (job_id, start_block, event_id) "
                "SELECT 'bench', 10, event_id FROM transfer_events"
            )
        )


def query_scale(settings, rows: int) -> dict:
    engine = engine_for(settings)
    try:
        with engine.begin() as connection:
            for table in (window_events, windows, transfers, jobs):
                connection.execute(table.delete())
        create_job(
            engine,
            "bench",
            TOKEN,
            source="sqd",
            start_block=10,
            window_size=5,
            parser_version=1,
            decimals=18,
        )
        with engine.begin() as connection:
            connection.execute(
                windows.insert().values(
                    job_id="bench",
                    start_block=10,
                    end_block=14,
                    status="done",
                    attempts=1,
                    updated_at_utc=datetime.now(),
                )
            )
            connection.execute(jobs.update().values(verified_through=14))
        began = time.perf_counter()
        seed(engine, rows)
        seeded = time.perf_counter() - began
        with engine.connect() as connection:
            job = load_job(connection, "bench")
        tracemalloc.start()
        began = time.perf_counter()
        try:
            result = summarize(engine, job, 10, 14)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert result["events"] == rows and result["unique_senders"] == rows
        return {
            "rows": rows,
            "distinct_addresses": 2 * rows,
            "insert_seconds": round(seeded, 1),
            "query_seconds": round(time.perf_counter() - began, 2),
            "python_peak_mib": round(peak / 2**20, 2),
        }
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--blocks", type=int, nargs="*", default=[1000, 20000, 50000])
    parser.add_argument("--mysql", action="store_true", help="also measure query")
    parser.add_argument("--rows", type=int, nargs="*", default=[10_000, 100_000])
    args = parser.parse_args()
    report = {"one_block": [one_block(n) for n in args.blocks]}
    if args.mysql:
        settings = replace(
            load_settings(), mysql_database="chainscope_bench_" + uuid4().hex[:12]
        )
        initialize(settings)
        try:
            report["query"] = [query_scale(settings, n) for n in args.rows]
        finally:
            server = engine_for(settings, server_only=True)
            try:
                with server.begin() as connection:
                    connection.execute(
                        sa.text(f"DROP DATABASE `{settings.mysql_database}`")
                    )
            finally:
                server.dispose()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
