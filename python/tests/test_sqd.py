"""SQD Portal client and cross-source totals, exercised with synthetic responses."""

import json

import httpx
import pytest

from chainscope.collector import transfer_totals
from chainscope.errors import ChainScopeError, SourceOverloaded
from chainscope.rpc_client import RpcClient
from chainscope.sqd import (
    OVERLOAD_PATIENCE,
    Portal,
    TooManyEvents,
    block_transfers,
    transfer_query,
    window_transfers,
)
from chainscope.validator import TRANSFER_TOPIC, Tally

TOKEN = "0x" + "1" * 40
FROM = "0x" + "2" * 40
TO = "0x" + "3" * 40
BLOCK_HASH = "0x" + "5" * 64


def tx(n: int) -> str:
    return "0x" + format(n, "064x")


def log(index: int, amount: int = 1, n: int = 1, **changes) -> dict:
    item = {
        "logIndex": index,
        "transactionHash": tx(n),
        "address": TOKEN,
        "topics": [
            TRANSFER_TOPIC,
            "0x" + "0" * 24 + FROM[2:],
            "0x" + "0" * 24 + TO[2:],
        ],
        "data": "0x" + format(amount, "064x"),
    }
    item.update(changes)
    return item


def block(number: int, *logs) -> dict:
    return {
        "header": {"number": number, "hash": BLOCK_HASH, "timestamp": 1_700_000_000},
        "logs": list(logs),
    }


def lines(*blocks) -> bytes:
    return b"".join(json.dumps(b).encode() + b"\n" for b in blocks)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self):
        return self.now


def portal(handler, clock=None, min_interval=0.0):
    clock = clock or Clock()
    return Portal(
        "https://portal.invalid/",
        "binance-mainnet",
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=clock.sleep,
        clock=clock,
        min_interval=min_interval,
    )


def stream_handler(responses, bodies):
    """Serve queued finalized-stream responses and record each request body."""

    def handler(request):
        bodies.append(json.loads(request.content))
        return responses.pop(0)

    return handler


def test_stream_resumes_after_last_received_block():
    def cut_off():
        yield lines(block(10, log(0)))
        raise httpx.ReadError("connection reset")

    bodies = []
    responses = [
        httpx.Response(200, content=cut_off()),
        httpx.Response(200, content=lines(block(12, log(0, n=2)))),
        httpx.Response(200, content=b""),
    ]
    client = portal(stream_handler(responses, bodies))
    got = [b["header"]["number"] for b in client.stream({}, 10, 20)]
    assert got == [10, 12]
    # Restart begins after block 10, never re-requesting or skipping a block.
    assert [b["fromBlock"] for b in bodies] == [10, 11, 13]
    assert {b["toBlock"] for b in bodies} == {20}
    assert client.stats["network_errors"] == 1


def test_stream_stops_when_range_is_complete():
    bodies = []
    responses = [httpx.Response(200, content=lines(block(10), block(11)))]
    client = portal(stream_handler(responses, bodies))
    assert len(list(client.stream({}, 10, 11))) == 2
    assert len(bodies) == 1


def test_retry_after_is_honoured():
    clock = Clock()
    responses = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, content=b""),
    ]
    client = portal(stream_handler(responses, []), clock)
    assert list(client.stream({}, 1, 2)) == []
    assert clock.sleeps == [7]
    assert client.stats["rate_limited"] == 1


def test_backoff_gives_up_after_repeated_failures():
    responses = [httpx.Response(503) for _ in range(10)]
    client = portal(stream_handler(responses, []))
    with pytest.raises(ChainScopeError, match="连续失败"):
        list(client.stream({}, 1, 2))


@pytest.mark.parametrize(
    ("status", "message"), [(409, "链重组"), (204, "高于数据集"), (400, "HTTP 400")]
)
def test_stream_rejects_non_retryable_status(status, message):
    client = portal(stream_handler([httpx.Response(status)], []))
    with pytest.raises(ChainScopeError, match=message):
        list(client.stream({}, 1, 2))


def test_stream_rejects_block_outside_range():
    responses = [httpx.Response(200, content=lines(block(99)))]
    client = portal(stream_handler(responses, []))
    with pytest.raises(ChainScopeError, match="不在请求范围"):
        list(client.stream({}, 1, 2))


def test_requests_are_paced():
    clock = Clock()
    responses = [
        httpx.Response(200, content=lines(block(1))),
        httpx.Response(200, content=b""),
    ]
    client = portal(stream_handler(responses, []), clock, min_interval=0.5)
    list(client.stream({}, 1, 5))
    assert clock.sleeps == [0.5]


def test_heads_and_time_lookup():
    def handler(request):
        path = request.url.path
        if path.endswith("/finalized-head"):
            return httpx.Response(200, json={"number": 100, "hash": BLOCK_HASH})
        if path.endswith("/timestamps/1700000000/block"):
            return httpx.Response(200, json={"block_number": 42})
        return httpx.Response(404)

    client = portal(handler)
    assert client.finalized_head() == 100
    assert client.block_at(1_700_000_000) == 42


def test_finalized_head_never_falls_back_to_unfinalized_head():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/finalized-head"):
            return httpx.Response(200, content=b"null")
        return httpx.Response(200, json={"number": 7})

    with pytest.raises(ChainScopeError, match="不改用未确认高度"):
        portal(handler).finalized_head()
    assert all(path.endswith("/finalized-head") for path in seen)


def test_block_transfers_decodes_standard_logs():
    events = block_transfers(block(10, log(3, amount=2**255)), TOKEN)
    assert events == [
        {
            "chain_id": 56,
            "token_address": TOKEN,
            "block_number": 10,
            "block_hash": BLOCK_HASH,
            "block_time_utc": events[0]["block_time_utc"],
            "tx_hash": tx(1),
            "log_index": 3,
            "from_address": FROM,
            "to_address": TO,
            "amount": 2**255,
        }
    ]
    assert events[0]["block_time_utc"].tzinfo is None


def test_time_lookup_retries_529_and_honors_retry_after():
    clock = Clock()
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if len(requests) == 1:
            return httpx.Response(529, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"block_number": 123})

    client = portal(handler, clock)
    assert client.block_at(1_791_907_200) == 123
    assert requests == [requests[0]] * 2
    assert clock.sleeps == [3]
    assert client.stats["server_errors"] == 1
    assert client.stats["requests"] == 2


def test_persistent_overload_waits_up_to_patience_then_stops():
    clock = Clock()
    client = portal(lambda _: httpx.Response(529, headers={"Retry-After": "10"}), clock)
    with pytest.raises(SourceOverloaded, match="持续过载"):
        client.block_at(1_791_907_200)
    # Waited as asked for the whole patience, not just seven attempts.
    assert clock.now == OVERLOAD_PATIENCE
    assert client.stats["requests"] == OVERLOAD_PATIENCE // 10 + 1
    assert client.stats["overloaded"] == OVERLOAD_PATIENCE // 10


def test_overload_patience_restarts_after_each_received_block():
    clock = Clock()
    responses = [
        httpx.Response(529, headers={"Retry-After": "120"}),
        httpx.Response(529, headers={"Retry-After": "120"}),
        httpx.Response(200, content=lines(block(10))),
        httpx.Response(529, headers={"Retry-After": "120"}),
        httpx.Response(529, headers={"Retry-After": "120"}),
        httpx.Response(200, content=b""),
    ]
    client = portal(stream_handler(responses, []), clock)
    got = [b["header"]["number"] for b in client.stream({}, 10, 20)]
    # 480 seconds of waiting in total, but never 300 without progress.
    assert got == [10]
    assert clock.sleeps == [120, 120, 120, 120]


def test_other_server_errors_keep_the_finite_retry_limit():
    client = portal(lambda _: httpx.Response(503))
    with pytest.raises(ChainScopeError, match="连续失败 7 次"):
        client.block_at(1_791_907_200)
    assert client.stats["requests"] == 7


def test_stream_also_retries_529_without_skipping_blocks():
    bodies = []
    responses = [
        httpx.Response(529),
        httpx.Response(200, content=lines(block(10, log(0)))),
    ]
    client = portal(stream_handler(responses, bodies))
    assert len(window_transfers(client, TOKEN, 10, 10, limit=10)) == 1
    assert [body["fromBlock"] for body in bodies] == [10, 10]


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"address": "0x" + "9" * 40}, "过滤条件以外"),
        ({"topics": [TRANSFER_TOPIC, "0x" + "0" * 64]}, "非标准"),
        ({"data": "0x01"}, "非标准"),
        ({"logIndex": -1}, "logIndex"),
    ],
)
def test_block_transfers_rejects_unexpected_logs(bad, message):
    with pytest.raises(ChainScopeError, match=message):
        block_transfers(block(10, log(0, **bad)), TOKEN)


def test_block_transfers_rejects_duplicate_log_index():
    with pytest.raises(ChainScopeError, match="重复"):
        block_transfers(block(10, log(0), log(0, n=2)), TOKEN)


def test_transfer_query_filters_token_and_topic():
    query = transfer_query(TOKEN)
    assert query["logs"] == [{"address": [TOKEN], "topic0": [TRANSFER_TOPIC]}]


def test_window_over_limit_asks_caller_to_split():
    def handler(request):
        return httpx.Response(200, content=lines(block(10, log(0), log(1))))

    with pytest.raises(TooManyEvents):
        window_transfers(portal(handler), TOKEN, 10, 20, limit=1)


def test_single_block_window_is_never_split():
    responses = [httpx.Response(200, content=lines(block(10, log(0), log(1))))]
    client = portal(stream_handler(responses, []))
    assert len(window_transfers(client, TOKEN, 10, 10, limit=1)) == 2


def add(tally, n, amount=5, sender=FROM, block_number=10):
    tally.add(tx(n), 0, sender, TO, amount, block_number)


def test_tally_is_order_independent():
    first, second = Tally(), Tally()
    for n in (1, 2, 3):
        add(first, n)
    for n in (3, 1, 2):
        add(second, n)
    assert second.matches(first.events, first.amount, first.fingerprint_bytes())
    assert len(first.fingerprint_bytes()) == 32


@pytest.mark.parametrize(
    "change",
    [
        lambda t: None,  # one event missing
        lambda t: add(t, 3, amount=6),  # different amount
        lambda t: add(t, 3, sender=TO),  # different sender
        lambda t: add(t, 4),  # a different event, same count and amount
        lambda t: add(t, 3, block_number=11),  # same event moved to another block
    ],
)
def test_tally_detects_missing_or_changed_events(change):
    expected, actual = Tally(), Tally()
    for n in (1, 2, 3):
        add(expected, n)
    for n in (1, 2):
        add(actual, n)
    change(actual)
    assert not actual.matches(
        expected.events, expected.amount, expected.fingerprint_bytes()
    )


def test_fingerprint_versions():
    """v2 adds the block number; v1 is kept to check windows stored before it."""
    old, new = Tally(version=1), Tally()
    old.add(tx(1), 0, FROM, TO, 5)
    new.add(tx(1), 0, FROM, TO, 5, 10)
    assert old.fingerprint_bytes() != new.fingerprint_bytes()
    moved_old, moved_new = Tally(version=1), Tally()
    moved_old.add(tx(1), 0, FROM, TO, 5, 11)  # v1 ignores the block number
    moved_new.add(tx(1), 0, FROM, TO, 5, 11)
    assert moved_old.fingerprint_bytes() == old.fingerprint_bytes()
    assert moved_new.fingerprint_bytes() != new.fingerprint_bytes()
    with pytest.raises(ChainScopeError, match="区块号"):
        Tally().add(tx(1), 0, FROM, TO, 5)


def alchemy_transfer(n, amount=5):
    return {
        "category": "erc20",
        "blockNum": "0xa",
        "hash": tx(n),
        "uniqueId": tx(n) + f":log:{n}",
        "from": FROM,
        "to": TO,
        "rawContract": {"value": hex(amount), "address": TOKEN},
    }


def alchemy(pages):
    def handler(request):
        body = json.loads(request.content)
        result = pages.pop(0)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    return RpcClient(
        "https://example.invalid/private-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )


def test_transfer_totals_match_portal_tally_across_pages():
    pages = [
        {"transfers": [alchemy_transfer(1), alchemy_transfer(2)], "pageKey": "p2"},
        # The provider repeats an event on the next page; it must count once.
        {"transfers": [alchemy_transfer(2), alchemy_transfer(3)]},
    ]
    with alchemy(pages) as rpc:
        tally, page_count = transfer_totals(rpc, TOKEN, 10, 10)
    portal_events = block_transfers(
        block(10, *(log(n, amount=5, n=n) for n in (1, 2, 3))), TOKEN
    )
    expected = Tally()
    for e in portal_events:
        expected.add(
            e["tx_hash"],
            e["log_index"],
            e["from_address"],
            e["to_address"],
            e["amount"],
            e["block_number"],
        )
    assert page_count == 2
    assert tally.matches(expected.events, expected.amount, expected.fingerprint_bytes())


def test_transfer_totals_rejects_page_limit_and_conflicts():
    with alchemy([{"transfers": [], "pageKey": "more"}]) as rpc:
        with pytest.raises(ChainScopeError, match="分页上限"):
            transfer_totals(rpc, TOKEN, 10, 10, max_pages=1)
    pages = [
        {"transfers": [alchemy_transfer(1)], "pageKey": "p2"},
        {"transfers": [alchemy_transfer(1, amount=6)]},
    ]
    with alchemy(pages) as rpc:
        with pytest.raises(ChainScopeError, match="内容冲突"):
            transfer_totals(rpc, TOKEN, 10, 10)
