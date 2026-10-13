"""Diagnostic output must stay bounded and preserve complete JSON files."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from chainscope.archive import JsonArchive
from chainscope.errors import ChainScopeError


def test_quota_rejects_write_without_damaging_previous_file(tmp_path):
    archive = JsonArchive(tmp_path / "batch", batch_limit=150, total_limit=1000)
    archive.write_json("manifest.json", {"status": "running"})
    before = (archive.directory / "manifest.json").read_bytes()
    with pytest.raises(ChainScopeError, match="单批容量"):
        archive.write_json("manifest.json", {"data": "x" * 200})
    assert (archive.directory / "manifest.json").read_bytes() == before
    assert json.loads(before)["status"] == "running"
    assert not list(archive.directory.glob(".pending-*.json"))


def test_total_quota_counts_existing_historical_files(tmp_path):
    historical = tmp_path / "old"
    historical.mkdir()
    (historical / "response.json").write_text('"' + "x" * 250 + '"', encoding="utf-8")
    archive = JsonArchive(tmp_path / "new", batch_limit=100, total_limit=300)
    with pytest.raises(ChainScopeError, match="总容量"):
        archive.write_json("response.json", {"data": "x" * 60})
    assert list(archive.directory.iterdir()) == []
    assert (historical / "response.json").is_file()


def test_concurrent_writes_share_one_capacity_limit(tmp_path):
    archives = [
        JsonArchive(tmp_path / f"batch-{index}", batch_limit=150, total_limit=300)
        for index in range(6)
    ]

    def write(archive):
        try:
            archive.write_json("response.json", {"data": "x" * 100})
        except ChainScopeError as exc:
            assert "总容量" in str(exc)
            return False
        return True

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(write, archives))
    assert sum(outcomes) == 2
    assert sum(p.stat().st_size for p in tmp_path.rglob("*") if p.is_file()) <= 300
    assert json.loads((tmp_path / ".archive-lock.json").read_text()) == {}
