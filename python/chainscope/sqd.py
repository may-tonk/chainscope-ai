"""SQD Portal client: paced finalized-stream requests that resume after the last block."""

import json
import time
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import httpx

from .dex import block_swaps, swap_log_filter
from .errors import ChainScopeError
from .parser import ADDRESS, HASH, fixed_hex, quantity
from .validator import TRANSFER_TOPIC

DEFAULT_PORTAL = "https://portal.sqd.dev"
DEFAULT_DATASET = "binance-mainnet"
# Bump when parsing rules change, so affected windows can be found and redone.
PARSER_VERSION = 1
RETRY_STATUS = (429, 500, 502, 503, 504)
MAX_LINE_CHARS = 8 * 1024 * 1024
BLOCK_FIELDS = {"number": True, "hash": True, "timestamp": True}
LOG_FIELDS = {
    "logIndex": True,
    "transactionHash": True,
    "address": True,
    "topics": True,
    "data": True,
}


class TooManyEvents(Exception):
    """A window holds more events than its limit; the caller splits it."""


class Portal:
    """Paced requests, Retry-After and resume after the last received block."""

    def __init__(
        self,
        base: str,
        dataset: str,
        client: httpx.Client,
        *,
        notify: Callable[[str], None] | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
        min_interval: float = 0.5,
    ):
        self._base = base.rstrip("/")
        self.dataset = dataset
        self._client = client
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

    def get(self, path: str):
        url = f"{self._base}/{path}"
        failures = 0
        while True:
            self._pace()
            try:
                response = self._client.get(url)
            except httpx.TransportError:
                failures += 1
                self._backoff(failures, path)
                continue
            if response.status_code in RETRY_STATUS:
                failures += 1
                retry_after = response.headers.get("Retry-After")
                self._backoff(failures, path, response.status_code, retry_after)
                continue
            if response.status_code != 200:
                raise ChainScopeError(f"Portal {path}: HTTP {response.status_code}")
            self.stats["bytes"] += response.num_bytes_downloaded
            try:
                return response.json()
            except ValueError:
                raise ChainScopeError(f"Portal {path}: 返回内容不是 JSON") from None

    def dataset_get(self, path: str):
        return self.get(f"datasets/{self.dataset}/{path}")

    def finalized_head(self) -> int:
        head = self.dataset_get("finalized-head") or self.dataset_get("head")
        number = head.get("number") if isinstance(head, dict) else None
        if not isinstance(number, int) or isinstance(number, bool):
            raise ChainScopeError("Portal 没有返回数据集最终确认高度")
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
        while cursor <= end:
            self._pace()
            body = {**query, "fromBlock": cursor, "toBlock": end}
            received = 0
            status = None
            retry_after = None
            try:
                with self._client.stream("POST", url, json=body) as response:
                    status = response.status_code
                    retry_after = response.headers.get("Retry-After")
                    if status == 200:
                        for line in response.iter_lines():
                            if len(line) > MAX_LINE_CHARS:
                                raise ChainScopeError(
                                    "Portal 单个区块响应超过 8 MiB 上限"
                                )
                            if not line.strip():
                                continue
                            block = portal_block(line)
                            number = block["header"]["number"]
                            if not cursor <= number <= end:
                                raise ChainScopeError("Portal 返回的区块不在请求范围内")
                            cursor = number + 1
                            received += 1
                            failures = 0
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
            if status in RETRY_STATUS:
                failures += 1
                self._backoff(failures, "stream", status, retry_after)
                continue
            if status == 204:
                raise ChainScopeError("Portal: 请求区间高于数据集当前高度（HTTP 204）")
            if status == 409:
                raise ChainScopeError("Portal: finalized 流返回链重组（HTTP 409）")
            raise ChainScopeError(f"Portal stream: HTTP {status}")


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
) -> tuple[list[dict], list[dict]]:
    """Transfers and tracked-pool swaps of one window in a single stream.

    Past `limit` Transfers, TooManyEvents is raised so the caller can split.
    """
    pools = pools or {}
    events, swap_legs = [], []
    blocks = portal.stream(transfer_query(token, swap_log_filter(pools)), start, end)
    try:
        for block in blocks:
            events.extend(block_transfers(block, token, ignore=pools.keys()))
            if pools:
                swap_legs.extend(block_swaps(block, pools))
            if len(events) > limit and end > start:
                raise TooManyEvents(len(events))
    finally:
        blocks.close()  # closes the open HTTP response when stopping early
    return events, swap_legs


def window_transfers(
    portal: Portal, token: str, start: int, end: int, limit: int
) -> list[dict]:
    """All Transfer events of one window; past `limit` raise TooManyEvents."""
    return window_activity(portal, token, start, end, limit)[0]
