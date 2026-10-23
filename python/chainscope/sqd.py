"""SQD Portal client: paced finalized-stream requests that resume after the last block."""

import json
import time
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import httpx

from .dex import block_swaps, swap_log_filter
from .errors import BlockTooLarge, ChainScopeError, SourceOverloaded
from .parser import ADDRESS, HASH, fixed_hex, quantity
from .rpc_client import read_limited
from .validator import TRANSFER_TOPIC

DEFAULT_PORTAL = "https://portal.sqd.dev"
DEFAULT_DATASET = "binance-mainnet"
# One version for both parsers that read a window's response (Transfer and
# Swap, which arrive in the same request). Bump it when either changes; each
# window records the version that actually parsed it, and `redo` without a
# range resets only windows below the current version.
PARSER_VERSION = 1
RETRY_STATUS = (429, 500, 502, 503, 504, 529)
# 529 is SQD's rate_limit_error/overloaded answer (with Retry-After) when the
# shared public Portal is at capacity; 429 is a per-client limit. Both mean
# "wait", not "broken": keep waiting as asked, and give up only after this
# many seconds pass without any successful response.
OVERLOAD_STATUS = (429, 529)
OVERLOAD_PATIENCE = 300
OVERLOAD_DEFAULT_WAIT = 10
# One line of the stream is one block. 32 MiB holds about 70,000 Transfer logs,
# more than BLOCK_HARD_LIMIT, so the item limit is what normally stops a block.
MAX_LINE_BYTES = 32 * 1024 * 1024
MAX_GET_BYTES = 1024 * 1024  # heads and time lookups are tiny JSON objects
# A block cannot be split, so a single-block window may pass event_limit up to
# this many Transfers plus swaps; beyond it the window is quarantined. Equal to
# the default event limit: no window ever holds more than about 50,000 items,
# measured at roughly 3 KB of Python memory each (about 140 MiB peak).
BLOCK_HARD_LIMIT = 50_000
BLOCK_FIELDS = {"number": True, "hash": True, "timestamp": True}
LOG_FIELDS = {
    "logIndex": True,
    "transactionHash": True,
    "address": True,
    "topics": True,
    "data": True,
}


class TooManyEvents(Exception):
    """A window holds more events than its limit; the caller splits it.

    `held` is the count when the limit was passed and `block` the block being
    read; both are None when one block's line was too long to read.
    """

    def __init__(self, held: int | None = None, block: int | None = None):
        super().__init__(held, block)
        self.held = held
        self.block = block


class LineTooLong(ChainScopeError):
    """One block's JSON line passed MAX_LINE_BYTES."""


class Portal:
    """Paced requests, Retry-After and resume after the last received block."""

    def __init__(
        self,
        base: str,
        dataset: str,
        client: httpx.Client,
        *,
        api_key: str = "",
        notify: Callable[[str], None] | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
        min_interval: float = 0.5,
        overload_patience: float = OVERLOAD_PATIENCE,
    ):
        self._base = base.rstrip("/")
        self._overload_patience = overload_patience
        self.dataset = dataset
        self._client = client
        # Testing and paid Portal plans take a key; the public Portal needs none.
        self._headers = {"x-api-key": api_key} if api_key else {}
        self._notify = notify or (lambda message: None)
        self._sleep = sleep
        self._clock = clock
        # The public portal allows 20 requests per 10 seconds.
        self._min_interval = min_interval
        self._last: float | None = None
        self.stats: Counter = Counter()

    def _pace(self) -> None:
        if self._last is not None:
            gap = self._min_interval - (self._clock() - self._last)
            if gap > 0:
                self._sleep(gap)
        self._last = self._clock()
        self.stats["requests"] += 1

    def _backoff(self, failures: int, what: str, status=None, retry_after=None):
        if failures > 6:
            raise ChainScopeError(f"Portal {what}: 连续失败 {failures} 次，停止")
        wait = min(2**failures, 60)
        if retry_after and retry_after.strip().isdigit():
            wait = min(int(retry_after.strip()), 120)
        kind = {None: "network_errors", 429: "rate_limited"}.get(
            status, "server_errors"
        )
        self.stats[kind] += 1
        self.stats["retries"] += 1
        self.stats["wait_seconds"] += wait
        reason = "网络中断" if status is None else f"HTTP {status}"
        self._notify(f"Portal {what}: {reason}，第 {failures} 次，{wait} 秒后重试")
        self._sleep(wait)

    def _throttle(self, since, what, status, retry_after):
        """Wait as an overloaded Portal asks; return when the waiting began.

        `since` is None at the first overload after a success. Stops with
        SourceOverloaded once the Portal has been overloaded for longer than
        the patience limit, so a busy Portal never looks like an empty range.
        """
        now = self._clock()
        since = now if since is None else since
        if now - since >= self._overload_patience:
            raise SourceOverloaded(
                f"Portal {what}: 持续过载 {int(now - since)} 秒仍未恢复"
                f"（HTTP {status}，服务端要求稍后重试），停止"
            )
        wait = OVERLOAD_DEFAULT_WAIT
        if retry_after and retry_after.strip().isdigit():
            wait = min(int(retry_after.strip()), 120)
        self.stats["rate_limited" if status == 429 else "server_errors"] += 1
        self.stats["overloaded"] += 1
        self.stats["retries"] += 1
        self.stats["wait_seconds"] += wait
        self._notify(f"Portal {what}: HTTP {status} 服务端过载，{wait} 秒后重试")
        self._sleep(wait)
        return since

    def get(self, path: str):
        url = f"{self._base}/{path}"
        failures = 0
        overloaded_since = None
        while True:
            self._pace()
            try:
                with self._client.stream("GET", url, headers=self._headers) as response:
                    status = response.status_code
                    retry_after = response.headers.get("Retry-After")
                    content = (
                        read_limited(
                            response, MAX_GET_BYTES, f"Portal {path}: 响应超过 1 MiB"
                        )
                        if status == 200
                        else b""
                    )
                    self.stats["bytes"] += response.num_bytes_downloaded
            except httpx.TransportError:
                failures += 1
                self._backoff(failures, path)
                continue
            if status in OVERLOAD_STATUS:
                overloaded_since = self._throttle(
                    overloaded_since, path, status, retry_after
                )
                continue
            if status in RETRY_STATUS:
                failures += 1
                self._backoff(failures, path, status, retry_after)
                continue
            if status != 200:
                raise ChainScopeError(f"Portal {path}: HTTP {status}")
            try:
                return json.loads(content)
            except ValueError:
                raise ChainScopeError(f"Portal {path}: 返回内容不是 JSON") from None

    def dataset_get(self, path: str):
        return self.get(f"datasets/{self.dataset}/{path}")

    def finalized_head(self) -> int:
        """The dataset's finalized height; never the unfinalized head instead."""
        head = self.dataset_get("finalized-head")
        number = head.get("number") if isinstance(head, dict) else None
        if not isinstance(number, int) or isinstance(number, bool):
            raise ChainScopeError(
                "Portal 没有返回数据集最终确认高度；不改用未确认高度，本次停止"
            )
        return number

    def block_at(self, timestamp: int) -> int:
        """First block whose timestamp is at or after the given Unix second."""
        found = self.dataset_get(f"timestamps/{timestamp}/block")
        number = found.get("block_number") if isinstance(found, dict) else None
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise ChainScopeError("Portal 时间换算区块返回格式错误")
        return number

    def stream(self, query: dict, start: int, end: int) -> Iterator[dict]:
        """Yield blocks in [start, end]; a cut-off response resumes at last + 1."""
        url = f"{self._base}/datasets/{self.dataset}/finalized-stream"
        cursor = start
        failures = 0
        overloaded_since = None
        while cursor <= end:
            self._pace()
            body = {**query, "fromBlock": cursor, "toBlock": end}
            received = 0
            status = None
            retry_after = None
            try:
                with self._client.stream(
                    "POST", url, json=body, headers=self._headers
                ) as response:
                    status = response.status_code
                    retry_after = response.headers.get("Retry-After")
                    if status == 200:
                        for line in bounded_lines(response, MAX_LINE_BYTES):
                            if not line.strip():
                                continue
                            block = portal_block(line)
                            number = block["header"]["number"]
                            if not cursor <= number <= end:
                                raise ChainScopeError("Portal 返回的区块不在请求范围内")
                            cursor = number + 1
                            received += 1
                            failures = 0
                            overloaded_since = None
                            yield block
                    self.stats["bytes"] += response.num_bytes_downloaded
            except (httpx.TransportError, httpx.DecodingError):
                failures += 1
                self._backoff(failures, "stream")
                continue
            if status == 200:
                if received == 0:
                    return  # Portal: empty body means no more matches in the range.
                continue
            if status in OVERLOAD_STATUS:
                overloaded_since = self._throttle(
                    overloaded_since, "stream", status, retry_after
                )
                continue
            if status in RETRY_STATUS:
                failures += 1
                self._backoff(failures, "stream", status, retry_after)
                continue
            if status == 204:
                raise ChainScopeError("Portal: 请求区间高于数据集当前高度（HTTP 204）")
            if status == 409:
                raise ChainScopeError("Portal: finalized 流返回链重组（HTTP 409）")
            raise ChainScopeError(f"Portal stream: HTTP {status}")


def bounded_lines(response: httpx.Response, limit: int) -> Iterator[str]:
    """Lines of a streamed body; a line is refused as soon as it passes `limit`.

    The check runs while reading, so an oversized block never sits whole in
    memory; leaving the generator closes the response.
    """
    buffer = bytearray()
    for chunk in response.iter_bytes():
        start = 0
        while True:
            end = chunk.find(b"\n", start)
            piece = chunk[start:] if end < 0 else chunk[start:end]
            if len(buffer) + len(piece) > limit:
                raise LineTooLong(f"Portal 单个区块响应超过 {limit // 2**20} MiB 上限")
            buffer += piece
            if end < 0:
                break
            yield buffer.decode()
            buffer.clear()
            start = end + 1
    if buffer:
        yield buffer.decode()


def portal_block(line: str) -> dict:
    try:
        block = json.loads(line)
    except ValueError:
        raise ChainScopeError("Portal 返回的行不是 JSON") from None
    header = block.get("header") if isinstance(block, dict) else None
    if (
        not isinstance(header, dict)
        or not isinstance(header.get("number"), int)
        or isinstance(header.get("number"), bool)
    ):
        raise ChainScopeError("Portal 区块缺少 header.number")
    return block


def transfer_query(token: str, swap_logs: list[dict] | None = None) -> dict:
    """Token Transfers, plus the Swap logs of tracked pools with their senders."""
    query = {
        "type": "evm",
        "fields": {"block": BLOCK_FIELDS, "log": LOG_FIELDS},
        "logs": [{"address": [token], "topic0": [TRANSFER_TOPIC]}],
    }
    if swap_logs:
        query["fields"]["transaction"] = {"hash": True, "from": True}
        query["logs"] += swap_logs
    return query


def block_transfers(block: dict, token: str, ignore=frozenset()) -> list[dict]:
    """Parse one Portal block into Transfer events, rejecting non-standard logs.

    ignore: addresses whose logs belong to another parser (tracked pools).
    """
    header = block["header"]
    stamp = header.get("timestamp")
    if not isinstance(stamp, int) or isinstance(stamp, bool) or stamp < 0:
        raise ChainScopeError("Portal 区块缺少 timestamp")
    number = header["number"]
    block_hash = fixed_hex(header.get("hash"), HASH, "block.hash")
    moment = datetime.fromtimestamp(stamp, UTC).replace(tzinfo=None)
    logs = block.get("logs") or []
    if not isinstance(logs, list):
        raise ChainScopeError("Portal 区块 logs 格式错误")
    events = []
    seen = set()
    for log in logs:
        topics = log.get("topics") if isinstance(log, dict) else None
        if (
            ignore
            and isinstance(log, dict)
            and str(log.get("address")).lower() in ignore
        ):
            continue
        if (
            not isinstance(topics, list)
            or not topics
            or fixed_hex(log.get("address"), ADDRESS, "log.address") != token
            or fixed_hex(topics[0], HASH, "topic0") != TRANSFER_TOPIC
        ):
            raise ChainScopeError("Portal 返回了过滤条件以外的日志")
        index = log.get("logIndex")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ChainScopeError("Portal 日志缺少 logIndex")
        if index in seen:
            raise ChainScopeError("Portal 同一区块返回了重复的 logIndex")
        seen.add(index)
        data = log.get("data")
        if len(topics) != 3 or not isinstance(data, str) or len(data) != 66:
            raise ChainScopeError(
                f"区块 {number} 有非标准 Transfer 日志（topics 或 data 长度不符）"
            )
        events.append(
            {
                "chain_id": 56,
                "token_address": token,
                "block_number": number,
                "block_hash": block_hash,
                "block_time_utc": moment,
                "tx_hash": fixed_hex(log.get("transactionHash"), HASH, "tx_hash"),
                "log_index": index,
                "from_address": "0x" + fixed_hex(topics[1], HASH, "topic1")[-40:],
                "to_address": "0x" + fixed_hex(topics[2], HASH, "topic2")[-40:],
                "amount": quantity(data, "Transfer.value"),
            }
        )
    return events


def window_activity(
    portal: Portal,
    token: str,
    start: int,
    end: int,
    limit: int,
    pools: dict | None = None,
    hard_limit: int = BLOCK_HARD_LIMIT,
) -> tuple[list[dict], list[dict]]:
    """Transfers and tracked-pool swaps of one window in a single stream.

    `limit` counts everything held in memory (Transfers plus swaps). Past it,
    TooManyEvents is raised so the caller can split the window. A one-block
    window cannot be split: it may hold up to `hard_limit`, then BlockTooLarge.
    """
    pools = pools or {}
    events, swap_legs = [], []
    blocks = portal.stream(transfer_query(token, swap_log_filter(pools)), start, end)
    try:
        for block in _blocks_or_split(blocks, start, end):
            events.extend(block_transfers(block, token, ignore=pools.keys()))
            if pools:
                swap_legs.extend(block_swaps(block, pools))
            held = len(events) + len(swap_legs)
            if held > limit and end > start:
                raise TooManyEvents(held, block["header"]["number"])
            if held > hard_limit:
                raise BlockTooLarge(
                    f"区块 {start} 有 {held} 条转账和交易，超过单区块硬上限 {hard_limit}；"
                    "已隔离该窗口，可调高 --block-hard-limit 后用 redo 重做"
                )
    finally:
        blocks.close()  # closes the open HTTP response when stopping early
    return events, swap_legs


def _blocks_or_split(blocks, start: int, end: int):
    """A block too long to read is too many items: split, or quarantine one block."""
    try:
        yield from blocks
    except LineTooLong as exc:
        if end > start:
            raise TooManyEvents(None) from None
        raise BlockTooLarge(
            f"区块 {start} 的响应超过单行上限（{exc}）；已隔离该窗口"
        ) from None


def window_transfers(
    portal: Portal, token: str, start: int, end: int, limit: int
) -> list[dict]:
    """All Transfer events of one window; past `limit` raise TooManyEvents."""
    return window_activity(portal, token, start, end, limit)[0]
