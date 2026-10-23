"""Opt-in tests against real MySQL; own temporary database, no user data."""

from dataclasses import replace
from datetime import datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from conftest import requires_mysql
from sqlalchemy.exc import DBAPIError
from test_collection import RAW, TOKEN, capture_rpc

from chainscope.config import load_settings
from chainscope.database import (
    fail_batch,
    start_batch,
    store_batch,
    verify_batch,
)
from chainscope.errors import ChainScopeError
from chainscope.models import batch_events, batches, transfers
from chainscope.parser import UINT256_MAX
from chainscope.pipeline import run_collection

pytestmark = requires_mysql


def event(amount=str(UINT256_MAX), tx="0x" + "4" * 64):
    return {
        "chain_id": 56,
        "token_address": "0x" + "1" * 40,
        "block_number": 10,
        "block_hash": "0x" + "5" * 64,
        "block_time_utc": datetime(2026, 1, 1),
        "tx_hash": tx,
        "log_index": 7,
        "from_address": "0x" + "2" * 40,
        "to_address": "0x" + "3" * 40,
        "amount_raw": amount,
        "token_decimals": 18,
    }


def new_batch(engine):
    batch_id = str(uuid4())
    start_batch(engine, batch_id, event()["token_address"], 10, 10, "synthetic-test")
    return batch_id


MANIFEST = {"end_block_hash": "0x" + "5" * 64, "receipt_verified_events": 1}


def test_exact_amount_repeat_and_conflict_rollback(mysql_engine):
    engine = mysql_engine
    first = new_batch(engine)
    assert store_batch(engine, first, [event()], MANIFEST)["inserted_count"] == 1
    assert verify_batch(engine, first)["amount_raw_sum"] == str(UINT256_MAX)
    second = new_batch(engine)
    assert store_batch(engine, second, [event()], MANIFEST) == {
        "inserted_count": 0,
        "existing_count": 1,
    }
    third = new_batch(engine)
    with pytest.raises(ChainScopeError, match="内容冲突"):
        # First insertion must roll back when the second event conflicts.
        store_batch(engine, third, [event("0", "0x" + "6" * 64), event("1")], MANIFEST)
    fail_batch(engine, third, "ChainScopeError")
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(transfers)) == 1
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(batch_events)
                .where(batch_events.c.batch_id == third)
            )
            == 0
        )
        assert (
            connection.scalar(
                sa.select(batches.c.status).where(batches.c.batch_id == third)
            )
            == "failed"
        )


@pytest.mark.parametrize("bad_amount", ["-1", "01", str(UINT256_MAX + 1)])
def test_database_uint256_constraints(mysql_engine, bad_amount):
    with pytest.raises(DBAPIError) as caught:
        with mysql_engine.begin() as connection:
            connection.execute(transfers.insert().values(**event(bad_amount)))
    # PyMySQL reports MySQL CHECK violations as OperationalError, code 3819.
    assert caught.value.orig.args[0] == 3819


def test_default_collection_persists_without_any_json(
    mysql_engine, monkeypatch, tmp_path
):
    import chainscope.pipeline as pipeline

    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(pipeline, "engine_for", lambda settings: mysql_engine)
    monkeypatch.setattr(
        pipeline, "RpcClient", lambda url: capture_rpc([{"transfers": [RAW]}])
    )
    settings = replace(load_settings(), rpc_url="https://example.invalid")
    first = run_collection(
        settings, TOKEN, 10, 10, persist=True, max_pages=20, receipt_limit=10
    )
    second = run_collection(
        settings, TOKEN, 10, 10, persist=True, max_pages=20, receipt_limit=10
    )
    assert first["inserted_count"] == 1
    assert second["inserted_count"] == 0 and second["existing_count"] == 1
    assert first["archive_enabled"] is False and first["raw_directory"] is None
    assert first["database_verification"]["amount_raw_sum"] == str(UINT256_MAX)
    assert list(tmp_path.iterdir()) == []
    with mysql_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0009"
        )
        assert connection.execute(
            sa.select(batches.c.raw_directory)
        ).scalars().all() == [None, None]
        assert connection.scalar(sa.select(sa.func.count()).select_from(transfers)) == 1


def prepare_pipeline(mysql_engine, monkeypatch, tmp_path):
    import chainscope.pipeline as pipeline

    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(pipeline, "engine_for", lambda settings: mysql_engine)
    rpc = capture_rpc([{"transfers": [RAW]}])
    monkeypatch.setattr(pipeline, "RpcClient", lambda url: rpc)
    return replace(load_settings(), rpc_url="https://example.invalid"), rpc


def test_explicit_archive_saves_only_rpc_and_summary(
    mysql_engine, monkeypatch, tmp_path
):
    import json

    settings, _ = prepare_pipeline(mysql_engine, monkeypatch, tmp_path)
    result = run_collection(
        settings,
        TOKEN,
        10,
        10,
        persist=True,
        max_pages=20,
        receipt_limit=10,
        save_raw=True,
    )
    directory = tmp_path / "raw" / result["batch_id"]
    assert result["archive_enabled"] is True
    assert result["raw_directory"] == str(directory)
    assert list(directory.glob("rpc-*.json"))
    assert not (directory / "events.json").exists()
    assert (
        json.loads((directory / "manifest.json").read_text())["status"] == "succeeded"
    )


def test_archive_limit_fails_before_rpc_without_event_writes(
    mysql_engine, monkeypatch, tmp_path
):
    settings, rpc = prepare_pipeline(mysql_engine, monkeypatch, tmp_path)
    with pytest.raises(ChainScopeError, match="单批容量"):
        run_collection(
            settings,
            TOKEN,
            10,
            10,
            persist=True,
            max_pages=20,
            receipt_limit=10,
            save_raw=True,
            archive_max_bytes=100,
        )
    assert rpc.requests == 0
    with mysql_engine.connect() as connection:
        assert connection.scalar(sa.select(batches.c.status)) == "failed"
        assert connection.scalar(sa.select(sa.func.count()).select_from(transfers)) == 0


def test_postcommit_archive_failure_does_not_mark_batch_failed(
    mysql_engine, monkeypatch, tmp_path
):
    from chainscope.archive import JsonArchive

    settings, _ = prepare_pipeline(mysql_engine, monkeypatch, tmp_path)
    original_write = JsonArchive.write_json

    def write(archive, name, value):
        if name == "manifest.json" and value["status"] == "succeeded":
            raise ChainScopeError("simulated archive quota failure")
        original_write(archive, name, value)

    monkeypatch.setattr(JsonArchive, "write_json", write)
    with pytest.raises(ChainScopeError, match="已提交 MySQL"):
        run_collection(
            settings,
            TOKEN,
            10,
            10,
            persist=True,
            max_pages=20,
            receipt_limit=10,
            save_raw=True,
        )
    with mysql_engine.connect() as connection:
        batch_id = connection.scalar(sa.select(batches.c.batch_id))
        assert connection.scalar(sa.select(batches.c.status)) == "succeeded"
    assert verify_batch(mysql_engine, batch_id)["event_count"] == 1
