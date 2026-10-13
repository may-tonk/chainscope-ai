"""Synthetic responses exercise failures without relying on a paid API."""

import copy
import json

import httpx
import pytest

from chainscope.collector import capture
from chainscope.errors import ChainScopeError
from chainscope.parser import UINT256_MAX, parse_transfer
from chainscope.rpc_client import RpcClient
from chainscope.validator import TRANSFER_TOPIC, verify_receipt

TOKEN = "0x" + "1" * 40
FROM = "0x" + "2" * 40
TO = "0x" + "3" * 40
TX = "0x" + "4" * 64
BLOCK_HASH = "0x" + "5" * 64
BLOCK = {"number": "0xa", "hash": BLOCK_HASH, "timestamp": "0x5f5e100"}
RAW = {
    "category": "erc20",
    "blockNum": "0xa",
    "hash": TX,
    "uniqueId": TX + ":log:7",
    "from": FROM,
    "to": TO,
    "value": 0.1,
    "rawContract": {"value": hex(UINT256_MAX), "decimal": "0x12", "address": TOKEN},
}


def receipt():
    return {
        "status": "0x1",
        "transactionHash": TX,
        "blockHash": BLOCK_HASH,
        "blockNumber": "0xa",
        "logs": [
            {
                "logIndex": "0x7",
                "transactionHash": TX,
                "blockHash": BLOCK_HASH,
                "blockNumber": "0xa",
                "address": TOKEN,
                "topics": [
                    TRANSFER_TOPIC,
                    "0x" + "0" * 24 + FROM[2:],
                    "0x" + "0" * 24 + TO[2:],
                ],
                "data": "0x" + format(UINT256_MAX, "064x"),
                "removed": False,
            }
        ],
    }


def rpc_client(handler):
    return RpcClient(
        "https://example.invalid/private-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )


def response(request, result):
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": json.loads(request.content)["id"],
            "result": result,
        },
    )


def test_uint256_preserved_and_float_ignored():
    event = parse_transfer(RAW, TOKEN, 10, 10, BLOCK)
    assert event["amount_raw"] == str(UINT256_MAX)
    verify_receipt(event, receipt())
    zero = copy.deepcopy(RAW)
    zero["rawContract"]["value"] = "0x0"
    zero["from"] = "0x" + "0" * 40
    assert parse_transfer(zero, TOKEN, 10, 10, BLOCK)["amount_raw"] == "0"


@pytest.mark.parametrize("value", [None, "123", "0x-1", hex(UINT256_MAX + 1)])
def test_invalid_raw_amount_fails(value):
    raw = copy.deepcopy(RAW)
    raw["rawContract"]["value"] = value
    with pytest.raises(ChainScopeError):
        parse_transfer(raw, TOKEN, 10, 10, BLOCK)


@pytest.mark.parametrize(
    "field,value",
    [
        ("blockNum", "0xb"),
        ("uniqueId", TX + ":external"),
        ("category", "erc721"),
        ("from", "bad"),
    ],
)
def test_invalid_transfer_fields_fail(field, value):
    raw = copy.deepcopy(RAW)
    raw[field] = value
    with pytest.raises(ChainScopeError):
        parse_transfer(raw, TOKEN, 10, 10, BLOCK)


def test_receipt_mismatch_and_removed_fail():
    event = parse_transfer(RAW, TOKEN, 10, 10, BLOCK)
    for changes in ({"data": "0x" + "0" * 64}, {"removed": True}, {"blockHash": TX}):
        bad = receipt()
        bad["logs"][0].update(changes)
        with pytest.raises(ChainScopeError):
            verify_receipt(event, bad)


def test_retry_and_secret_not_in_error():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(429) if len(attempts) < 3 else response(request, "0x38")

    rpc = rpc_client(handler)
    assert rpc.call("eth_chainId", []) == "0x38"
    assert rpc.requests == 3
    failing = rpc_client(
        lambda request: httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32601, "message": "private-key"},
            },
        )
    )
    with pytest.raises(ChainScopeError) as caught:
        failing.call("bad_method", [])
    assert "private-key" not in str(caught.value)


@pytest.mark.parametrize("failure_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_retry_is_bounded(failure_type):
    def handler(request):
        raise failure_type("private-key", request=request)

    rpc = rpc_client(handler)
    with pytest.raises(ChainScopeError) as caught:
        rpc.call("eth_chainId", [])
    assert rpc.requests == 3
    assert "private-key" not in str(caught.value)


def capture_rpc(pages, *, after_hash=BLOCK_HASH):
    responses = iter(pages)
    anchors = []

    def handler(request):
        data = json.loads(request.content)
        method = data["method"]
        if method == "eth_chainId":
            result = "0x38"
        elif method == "eth_blockNumber":
            result = "0x64"
        elif method == "eth_getCode":
            result = "0x6080"
        elif method == "eth_getBlockByNumber":
            anchors.append(1)
            result = {**BLOCK, "hash": BLOCK_HASH if len(anchors) == 1 else after_hash}
        elif method == "eth_getTransactionReceipt":
            result = receipt()
        elif method == "alchemy_getAssetTransfers":
            assert data["params"][0]["toBlock"] == "0xa"
            assert data["params"][0]["excludeZeroValue"] is False
            result = next(responses)
        else:
            raise AssertionError(method)
        return response(request, result)

    return rpc_client(handler)


def test_two_pages_deduplicate_and_archive(tmp_path):
    rpc = capture_rpc([{"transfers": [RAW], "pageKey": "next"}, {"transfers": [RAW]}])
    directory = tmp_path / "batch"
    events, manifest = capture(rpc, TOKEN, 10, 10, directory)
    assert len(events) == 1
    assert manifest["pages"] == 2
    assert manifest["source_duplicate_count"] == 1
    assert manifest["receipt_verified_events"] == 1
    assert manifest["status"] == "verified_not_persisted"
    assert manifest["archive_enabled"] is True
    assert not (directory / "events.json").exists()
    assert "private-key" not in "".join(p.read_text() for p in directory.glob("*.json"))


def test_capture_without_archive_still_verifies(monkeypatch, tmp_path):
    def forbidden_archive(*args, **kwargs):
        raise AssertionError("default collection must not create an archive")

    monkeypatch.setattr("chainscope.collector.JsonArchive", forbidden_archive)
    events, manifest = capture(
        capture_rpc([{"transfers": [RAW]}]), TOKEN, 10, 10, batch_id="no-files"
    )
    assert len(events) == 1
    assert events[0]["amount_raw"] == str(UINT256_MAX)
    assert manifest["receipt_verified_events"] == 1
    assert manifest["archive_enabled"] is False
    bad = copy.deepcopy(RAW)
    bad["rawContract"]["value"] = "0x0"
    with pytest.raises(ChainScopeError, match="金额不一致"):
        capture(capture_rpc([{"transfers": [bad]}]), TOKEN, 10, 10, batch_id="bad")
    assert list(tmp_path.iterdir()) == []


def test_empty_success_is_distinct_from_missing_result(tmp_path):
    events, manifest = capture(
        capture_rpc([{"transfers": []}]), TOKEN, 10, 10, tmp_path / "empty"
    )
    assert events == [] and manifest["event_count"] == 0
    with pytest.raises(ChainScopeError):
        capture(capture_rpc([{}]), TOKEN, 10, 10, tmp_path / "missing")


@pytest.mark.parametrize(
    "pages,limit",
    [
        (
            [
                {"transfers": [], "pageKey": "same"},
                {"transfers": [], "pageKey": "same"},
            ],
            3,
        ),
        ([{"transfers": [], "pageKey": "next"}], 1),
    ],
)
def test_incomplete_pagination_marks_failed(tmp_path, pages, limit):
    directory = tmp_path / "failed"
    with pytest.raises(ChainScopeError):
        capture(capture_rpc(pages), TOKEN, 10, 10, directory, max_pages=limit)
    assert json.loads((directory / "manifest.json").read_text())["status"] == "failed"


def test_changed_anchor_fails(tmp_path):
    with pytest.raises(ChainScopeError, match="发生变化"):
        capture(
            capture_rpc([{"transfers": [RAW]}], after_hash=TX),
            TOKEN,
            10,
            10,
            tmp_path / "reorg",
        )


def test_conflicting_duplicate_fails(tmp_path):
    altered = copy.deepcopy(RAW)
    altered["rawContract"]["value"] = "0x0"
    with pytest.raises(ChainScopeError, match="内容冲突"):
        capture(
            capture_rpc([{"transfers": [RAW, altered]}]),
            TOKEN,
            10,
            10,
            tmp_path / "conflict",
        )
