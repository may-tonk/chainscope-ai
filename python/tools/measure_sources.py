"""M0: measure SQD Portal and Alchemy on real BSC data before building the pipeline.

Read-only: no MySQL access and no table changes. The JSON report is written to
reports/, which git ignores. Provider URLs and keys are never printed.
"""

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx

from chainscope.config import ROOT, load_settings
from chainscope.errors import ChainScopeError
from chainscope.parser import ADDRESS, HASH, fixed_hex, quantity
from chainscope.rpc_client import RpcClient
from chainscope.validator import TRANSFER_TOPIC

BEIJING = timezone(timedelta(hours=8))
ZERO_ADDRESS = "0x" + "0" * 40
# BEM contract confirmed by the user on 2026-10-15.
BEM_CANDIDATES = ("0x5ce033b2bfca3af30b3e8c8457deaf776a8b695a",)
# USDT is only a load test: the densest BSC token, never stored or tracked.
USDT = "0x55d398326f99059ff775485246999027b3197955"
TARGET_EVENTS = 100_000_000
FREE_CU_PER_MONTH = 30_000_000
# Alchemy official compute-unit costs, checked 2026-10-14.
ALCHEMY_CU = {
    "alchemy_getAssetTransfers": 120,
    "eth_blockNumber": 10,
    "eth_call": 26,
    "eth_chainId": 0,
    "eth_getBlockByNumber": 20,
    "eth_getCode": 20,
}
SELECTORS = {
    "name": "0x06fdde03",
    "symbol": "0x95d89b41",
    "decimals": "0x313ce567",
    "totalSupply": "0x18160ddd",
}
RETRY_STATUS = (429, 500, 502, 503, 504)
BLOCK_FIELDS = {"number": True, "hash": True, "timestamp": True}
LOG_FIELDS = {
    "logIndex": True,
    "transactionHash": True,
    "address": True,
    "topics": True,
    "data": True,
}


def say(message: str) -> None:
    stamp = datetime.now(BEIJING).strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", file=sys.stderr, flush=True)


def beijing_day(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, BEIJING).date().isoformat()


class Portal:
    """SQD Portal client: paced requests, Retry-After, resume after the last block."""

    def __init__(
        self,
        base: str,
        dataset: str,
        client: httpx.Client,
        *,
        sleep=time.sleep,
        clock=time.monotonic,
        min_interval: float = 0.5,
    ):
        self._base = base.rstrip("/")
        self.dataset = dataset
        self._client = client
        self._sleep = sleep
        self._clock = clock
        # Public portal allows 20 requests per 10 seconds.
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
        say(f"Portal {what}: {reason}，第 {failures} 次，{wait} 秒后重试")
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


class Alchemy:
    """Pace and count every Alchemy call so the report can show compute units."""

    def __init__(
        self,
        rpc: RpcClient,
        *,
        sleep=time.sleep,
        clock=time.monotonic,
        min_interval=0.3,
    ):
        self._rpc = rpc
        self._sleep = sleep
        self._clock = clock
        self._min_interval = min_interval
        self._last: float | None = None
        self.calls: Counter = Counter()

    def call(self, method: str, params: list):
        if self._last is not None:
            gap = self._min_interval - (self._clock() - self._last)
            if gap > 0:
                self._sleep(gap)
        self._last = self._clock()
        self.calls[method] += 1
        return self._rpc.call(method, params)

    def usage(self) -> dict:
        return {
            "calls": dict(self.calls),
            "http_requests_including_retries": self._rpc.requests,
            "estimated_cu": sum(
                ALCHEMY_CU.get(m, 0) * n for m, n in self.calls.items()
            ),
        }


class Tally:
    """Order-independent totals: two sources compare without holding the events."""

    def __init__(self):
        self.events = 0
        self.amount = 0
        self.fingerprint = 0
        self.minted = 0
        self.burned = 0
        self.days: Counter = Counter()
        self.first_block: int | None = None
        self.last_block: int | None = None

    def add(self, block, day, tx_hash, log_index, sender, receiver, amount) -> None:
        self.events += 1
        self.amount += amount
        key = f"{tx_hash}:{log_index}:{sender}:{receiver}:{amount}".encode()
        digest = int.from_bytes(hashlib.sha256(key).digest())
        self.fingerprint = (self.fingerprint + digest) % 2**256
        if sender == ZERO_ADDRESS:
            self.minted += amount
        if receiver == ZERO_ADDRESS:
            self.burned += amount
        self.days[day] += 1
        if self.first_block is None:
            self.first_block = block
        self.last_block = block

    def summary(self) -> dict:
        return {
            "events": self.events,
            "amount_sum": str(self.amount),
            "fingerprint": f"{self.fingerprint:064x}",
            "first_block": self.first_block,
            "last_block": self.last_block,
            "minted_from_zero": str(self.minted),
            "burned_to_zero": str(self.burned),
        }


def compare(sqd: Tally, alchemy: Tally) -> dict:
    days = sorted(set(sqd.days) | set(alchemy.days))
    differing = {
        day: {"sqd": sqd.days[day], "alchemy": alchemy.days[day]}
        for day in days
        if sqd.days[day] != alchemy.days[day]
    }
    return {
        "sqd_events": sqd.events,
        "alchemy_events": alchemy.events,
        "same_count": sqd.events == alchemy.events,
        "same_amount_sum": sqd.amount == alchemy.amount,
        "same_fingerprint": sqd.fingerprint == alchemy.fingerprint,
        "differing_days": dict(list(differing.items())[:20]),
    }


def density(tally: Tally, start_time: int, end_time: int) -> dict:
    """Daily counts over Beijing calendar days, zero-event days included.

    start_time and end_time are the timestamps of the first and last measured
    blocks. A day is full only when the range covers it from 00:00:00 to
    23:59:59; the others are partial and kept out of averages. "Last 7 days"
    means the last seven full calendar days, not the last seven active ones.
    """
    first_day = datetime.fromtimestamp(start_time, BEIJING).date()
    last_day = datetime.fromtimestamp(end_time, BEIJING).date()
    calendar = []
    day = first_day
    while day <= last_day:
        calendar.append(day)
        day += timedelta(days=1)

    def covered(day) -> bool:
        begin = datetime(day.year, day.month, day.day, tzinfo=BEIJING).timestamp()
        # Blocks come every second or faster, so a last block at 23:59:59 or
        # later means the day was covered to its end.
        return start_time <= begin and end_time >= begin + 86_399

    per_day = {d.isoformat(): tally.days.get(d.isoformat(), 0) for d in calendar}
    full = [d.isoformat() for d in calendar if covered(d)]
    partial = [d.isoformat() for d in calendar if not covered(d)]
    recent = full[-7:]

    def average(days):
        return round(sum(per_day[d] for d in days) / len(days), 1) if days else None

    result = {
        "calendar_days": len(calendar),
        "full_days": len(full),
        "partial_days": partial,
        "active_days": sum(1 for count in per_day.values() if count),
        "zero_event_full_days": sum(1 for d in full if not per_day[d]),
        "per_day": per_day,
        "busiest_day": max(per_day, key=per_day.__getitem__) if tally.events else None,
        "busiest_day_events": max(per_day.values(), default=0),
        "full_day_average": average(full),
        "last_7_full_days": recent,
        "last_7_full_days_average": average(recent),
    }
    if len(recent) < 7:
        result["note"] = f"完整自然日只有 {len(recent)} 天，近 7 天日均按这些天计算"
    if not full:
        result["note"] = "没有完整自然日，不能给出日均；见 per_day 的部分日计数"
    return result


def sqd_transfers(
    portal: Portal, token: str, start: int, end: int, label: str, cut: int | None = None
) -> tuple[Tally, Tally, dict]:
    query = {
        "type": "evm",
        "fields": {"block": BLOCK_FIELDS, "log": LOG_FIELDS},
        "logs": [{"address": [token], "topic0": [TRANSFER_TOPIC]}],
    }
    full, upto = Tally(), Tally()
    before = Counter(portal.stats)
    blocks = nonstandard = duplicates = 0
    began = shown = time.monotonic()
    say(f"{label} SQD：开始读取区块 {start:,} 到 {end:,}")
    for block in portal.stream(query, start, end):
        number = block["header"]["number"]
        stamp = block["header"].get("timestamp")
        if not isinstance(stamp, int) or isinstance(stamp, bool):
            raise ChainScopeError("Portal 区块缺少 timestamp")
        day = beijing_day(stamp)
        blocks += 1
        seen = set()
        for log in block.get("logs") or []:
            topics = log.get("topics") if isinstance(log, dict) else None
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
                duplicates += 1
                continue
            seen.add(index)
            data = log.get("data")
            if len(topics) != 3 or not isinstance(data, str) or len(data) != 66:
                nonstandard += 1
                continue
            event = (
                number,
                day,
                fixed_hex(log.get("transactionHash"), HASH, "transactionHash"),
                index,
                "0x" + fixed_hex(topics[1], HASH, "topic1")[-40:],
                "0x" + fixed_hex(topics[2], HASH, "topic2")[-40:],
                quantity(data, "log.data"),
            )
            full.add(*event)
            if cut is not None and number <= cut:
                upto.add(*event)
        now = time.monotonic()
        if now - shown >= 10:
            shown = now
            done = (number - start + 1) / (end - start + 1) * 100
            say(f"{label} SQD：区块 {number:,}（{done:.1f}%），事件 {full.events:,}")
    elapsed = time.monotonic() - began
    used = {key: portal.stats[key] - before.get(key, 0) for key in portal.stats}
    say(f"{label} SQD：完成，事件 {full.events:,}，用时 {elapsed:.0f} 秒")
    return (
        full,
        upto,
        {
            "range": [start, end],
            "elapsed_seconds": round(elapsed, 1),
            "events_per_second": round(full.events / elapsed, 1) if elapsed else None,
            "blocks_scanned_per_second": round((end - start + 1) / elapsed)
            if elapsed
            else None,
            "blocks_with_transfers": blocks,
            "nonstandard_transfer_logs": nonstandard,
            "duplicate_log_index": duplicates,
            "requests": used.get("requests", 0),
            "retries": used.get("retries", 0),
            "rate_limited": used.get("rate_limited", 0),
            "wait_seconds": round(used.get("wait_seconds", 0), 1),
            "bytes_downloaded": used.get("bytes", 0),
        },
    )


def alchemy_event(raw, token: str, start: int, end: int) -> tuple:
    if not isinstance(raw, dict):
        raise ChainScopeError("Transfers 记录不是对象")
    contract = raw.get("rawContract")
    if not isinstance(contract, dict):
        raise ChainScopeError("缺少 rawContract")
    if fixed_hex(contract.get("address"), ADDRESS, "rawContract.address") != token:
        raise ChainScopeError("Transfers 合约地址不符")
    number = quantity(raw.get("blockNum"), "blockNum", 2**64 - 1)
    if not start <= number <= end:
        raise ChainScopeError("Transfers 区块超出请求范围")
    tx_hash = fixed_hex(raw.get("hash"), HASH, "hash")
    uid = raw.get("uniqueId")
    match = (
        re.fullmatch(re.escape(tx_hash) + r":log:([0-9]+)", uid.lower())
        if isinstance(uid, str)
        else None
    )
    if not match:
        raise ChainScopeError("uniqueId 缺少日志索引")
    metadata = raw.get("metadata")
    stamp = metadata.get("blockTimestamp") if isinstance(metadata, dict) else None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ChainScopeError("Transfers 时间戳格式错误") from None
    if moment.tzinfo is None:
        raise ChainScopeError("Transfers 时间戳缺少时区")
    return (
        number,
        moment.astimezone(BEIJING).date().isoformat(),
        tx_hash,
        int(match[1]),
        fixed_hex(raw.get("from"), ADDRESS, "from"),
        fixed_hex(raw.get("to"), ADDRESS, "to"),
        quantity(contract.get("value"), "rawContract.value"),
    )


def transfers_page(alchemy: Alchemy, params: dict, label: str, page: int, sleep):
    for attempt in range(4):
        try:
            return alchemy.call("alchemy_getAssetTransfers", [params])
        except ChainScopeError as exc:
            if attempt == 3:
                raise
            wait = 10 * 2**attempt  # 70 s total stays inside the 10 min pageKey life
            say(
                f"{label} Alchemy 第 {page} 页失败（{exc}），{wait} 秒后用同一 pageKey 重试"
            )
            sleep(wait)
    raise AssertionError("unreachable retry state")


def alchemy_transfers(
    alchemy: Alchemy,
    token: str,
    start: int,
    end: int,
    label: str,
    max_pages: int,
    sleep=time.sleep,
) -> tuple[Tally, dict]:
    tally = Tally()
    pending: list[tuple] = []
    pending_keys: set = set()
    pending_block: int | None = None
    pages = duplicates = 0
    page_key = None
    anomalies: Counter = Counter()
    examples: list[str] = []
    began = shown = time.monotonic()
    say(f"{label} Alchemy：开始读取区块 {start:,} 到 {end:,}")
    while True:
        params = {
            "fromBlock": hex(start),
            "toBlock": hex(end),
            "contractAddresses": [token],
            "category": ["erc20"],
            "excludeZeroValue": False,
            "withMetadata": True,
            "maxCount": "0x3e8",
            "order": "asc",
        }
        if page_key:
            params["pageKey"] = page_key
        result = transfers_page(alchemy, params, label, pages + 1, sleep)
        pages += 1
        transfers = result.get("transfers") if isinstance(result, dict) else None
        if not isinstance(transfers, list):
            raise ChainScopeError("Transfers 返回格式错误")
        for raw in transfers:
            try:
                event = alchemy_event(raw, token, start, end)
            except ChainScopeError as exc:
                anomalies[str(exc)] += 1
                if len(examples) < 5 and isinstance(raw, dict):
                    examples.append(str(raw.get("uniqueId")))
                continue
            if pending_block is not None and event[0] < pending_block:
                raise ChainScopeError("Transfers 返回的区块顺序倒退")
            if event[0] != pending_block:
                for item in pending:
                    tally.add(*item)
                pending, pending_keys, pending_block = [], set(), event[0]
            if (event[2], event[3]) in pending_keys:
                duplicates += 1
                continue
            pending_keys.add((event[2], event[3]))
            pending.append(event)
        page_key = result.get("pageKey")
        if not page_key:
            for item in pending:
                tally.add(*item)
            covered_to, complete = end, True
            break
        if pages >= max_pages:
            # The last block may continue on the next page, so leave it out.
            covered_to = pending_block - 1 if pending_block is not None else start - 1
            complete = False
            break
        now = time.monotonic()
        if now - shown >= 10:
            shown = now
            say(
                f"{label} Alchemy：第 {pages} 页，区块 {pending_block or 0:,}，事件 {tally.events:,}"
            )
    elapsed = time.monotonic() - began
    say(
        f"{label} Alchemy：完成，{pages} 页，事件 {tally.events:,}，用时 {elapsed:.0f} 秒"
    )
    return tally, {
        "range": [start, end],
        "complete": complete,
        "covered_to_block": covered_to,
        "pages": pages,
        "estimated_cu": pages * ALCHEMY_CU["alchemy_getAssetTransfers"],
        "elapsed_seconds": round(elapsed, 1),
        "events_per_second": round(tally.events / elapsed, 1) if elapsed else None,
        "duplicate_events": duplicates,
        "anomalies": dict(anomalies),
        "anomaly_examples": examples,
    }


def header(alchemy: Alchemy, tag: str) -> dict:
    block = alchemy.call("eth_getBlockByNumber", [tag, False])
    if not isinstance(block, dict):
        raise ChainScopeError(f"区块 {tag} 不存在")
    return {
        "number": quantity(block.get("number"), "number", 2**64 - 1),
        "hash": fixed_hex(block.get("hash"), HASH, "hash"),
        "timestamp": quantity(block.get("timestamp"), "timestamp", 2**64 - 1),
    }


def code_at(alchemy: Alchemy, token: str, number: int) -> str:
    code = alchemy.call("eth_getCode", [token, hex(number)])
    if not isinstance(code, str) or not re.fullmatch(r"0x[0-9a-fA-F]*", code):
        raise ChainScopeError("eth_getCode 返回格式错误")
    return code.lower()


def deploy_block(alchemy: Alchemy, token: str, low: int, high: int) -> tuple[int, str]:
    """Binary-search the first block whose state has this contract's code."""
    if len(code_at(alchemy, token, low)) > 2:
        return low, "数据集起点已有合约代码，部署更早"
    while high - low > 1:
        middle = (low + high) // 2
        if len(code_at(alchemy, token, middle)) > 2:
            high = middle
        else:
            low = middle
    return high, "eth_getCode 二分查找"


def abi_text(raw) -> str:
    if not isinstance(raw, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", raw):
        raise ChainScopeError("eth_call 返回格式错误")
    data = bytes.fromhex(raw[2:])
    if len(data) == 32:
        return data.rstrip(b"\0").decode("utf-8", "replace")
    if len(data) >= 64:
        offset = int.from_bytes(data[:32])
        if offset + 32 <= len(data):
            length = int.from_bytes(data[offset : offset + 32])
            if offset + 32 + length <= len(data):
                return data[offset + 32 : offset + 32 + length].decode(
                    "utf-8", "replace"
                )
    raise ChainScopeError("eth_call 字符串长度不符")


def token_info(alchemy: Alchemy, token: str, number: int) -> dict:
    info: dict = {"at_block": number}
    for name, selector in SELECTORS.items():
        try:
            raw = alchemy.call(
                "eth_call", [{"to": token, "data": selector}, hex(number)]
            )
            if name in ("name", "symbol"):
                info[name] = abi_text(raw)
            elif name == "decimals":
                info[name] = quantity(raw, name, 255)
            else:
                info[name] = str(quantity(raw, name))
        except ChainScopeError as exc:
            info[name] = None
            info.setdefault("errors", {})[name] = str(exc)
    return info


def portal_header(portal: Portal, number: int) -> dict:
    query = {"type": "evm", "includeAllBlocks": True, "fields": {"block": BLOCK_FIELDS}}
    blocks = list(portal.stream(query, number, number))
    if len(blocks) != 1:
        raise ChainScopeError(f"Portal 没有返回区块 {number}")
    return blocks[0]["header"]


def compare_hashes(portal: Portal, alchemy: Alchemy, numbers: list[int]) -> dict:
    checked = {}
    for number in numbers:
        ours = portal_header(portal, number)
        theirs = header(alchemy, hex(number))
        checked[str(number)] = {
            "same_hash": str(ours.get("hash", "")).lower() == theirs["hash"],
            "same_timestamp": ours.get("timestamp") == theirs["timestamp"],
        }
    return checked


def first_block_at(portal: Portal, alchemy: Alchemy, instant: int) -> tuple[int, dict]:
    found = portal.dataset_get(f"timestamps/{instant}/block")
    number = found.get("block_number") if isinstance(found, dict) else None
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ChainScopeError("Portal 时间换算区块返回格式错误")
    at = header(alchemy, hex(number))
    before = header(alchemy, hex(number - 1))
    return number, {
        "instant_beijing": datetime.fromtimestamp(instant, BEIJING).isoformat(),
        "block": number,
        "block_timestamp": at["timestamp"],
        "previous_block_timestamp": before["timestamp"],
        "matches_alchemy": before["timestamp"] < instant <= at["timestamp"],
    }


def check_sources(portal: Portal, alchemy: Alchemy, dataset: str) -> dict:
    if alchemy.call("eth_chainId", []) != "0x38":
        raise ChainScopeError("BSC_RPC_URL 不是 BSC 主网（chainId 56）")
    names = {}
    for item in portal.get("datasets"):
        if isinstance(item, dict) and isinstance(item.get("dataset"), str):
            for name in [item["dataset"], *(item.get("aliases") or [])]:
                names[name] = item
    if dataset not in names:
        similar = sorted(n for n in names if re.search("bsc|bnb|binance", n))
        raise ChainScopeError(f"Portal 没有数据集 {dataset}；相近名称：{similar}")
    portal.dataset = names[dataset]["dataset"]
    metadata = portal.dataset_get("metadata")
    sqd_head = portal.dataset_get("head")
    sqd_final = portal.dataset_get("finalized-head")
    latest = quantity(alchemy.call("eth_blockNumber", []), "eth_blockNumber", 2**64 - 1)
    tags: dict = {}
    for tag in ("safe", "finalized"):
        try:
            tags[tag] = header(alchemy, tag)["number"]
        except ChainScopeError as exc:
            tags[tag] = f"不支持：{exc}"
    sqd_live = (sqd_head or {}).get("number")
    sqd_ready = (sqd_final or sqd_head or {}).get("number")
    if not isinstance(sqd_ready, int):
        raise ChainScopeError("Portal 没有返回数据集高度")
    alchemy_ready = tags["finalized"]
    if not isinstance(alchemy_ready, int):
        alchemy_ready = latest - 64
    return {
        "dataset": portal.dataset,
        "real_time": metadata.get("real_time") if isinstance(metadata, dict) else None,
        "start_block": metadata.get("start_block")
        if isinstance(metadata, dict)
        else None,
        "sqd_head": sqd_head,
        "sqd_finalized_head": sqd_final,
        "alchemy_latest": latest,
        "alchemy_tags": tags,
        "sqd_head_lag_blocks": latest - sqd_live if isinstance(sqd_live, int) else None,
        "sqd_finalized_lag_blocks": latest - sqd_ready,
        "alchemy_finalized_lag_blocks": latest - alchemy_ready,
        "measure_end_block": min(sqd_ready, alchemy_ready),
    }


def measure_token(
    portal: Portal, alchemy: Alchemy, result: dict, low: int, end: int, max_pages: int
) -> None:
    """Fill result step by step, so a failure still leaves what was measured."""
    token = result["address"]
    label = f"BEM {token[:8]}"
    code = code_at(alchemy, token, end)
    result["code_bytes"] = (len(code) - 2) // 2
    if len(code) <= 2 or code.startswith("0xef0100"):
        result["note"] = "该地址在测量终点没有合约代码，跳过"
        return
    result["token"] = token_info(alchemy, token, end)
    try:
        start, how = deploy_block(alchemy, token, low, end)
    except ChainScopeError as exc:
        start, how = low, f"二分查找失败（{exc}），从数据集起点扫描"
    result["range"] = {"start": start, "end": end, "start_from": how}
    alchemy_tally, result["alchemy"] = alchemy_transfers(
        alchemy, token, start, end, label, max_pages
    )
    result["alchemy_totals"] = alchemy_tally.summary()
    full, upto, result["sqd"] = sqd_transfers(
        portal, token, start, end, label, result["alchemy"]["covered_to_block"]
    )
    result["sqd_totals"] = full.summary()
    result["reconcile"] = compare(upto, alchemy_tally)
    result["reconcile"]["through_block"] = result["alchemy"]["covered_to_block"]
    result["density"] = density(
        full,
        header(alchemy, hex(start))["timestamp"],
        header(alchemy, hex(end))["timestamp"],
    )
    supply = result["token"].get("totalSupply")
    if supply is not None and how == "eth_getCode 二分查找":
        result["supply_check"] = {
            "total_supply": supply,
            "minted_minus_burned": str(full.minted - full.burned),
            "equal": int(supply) == full.minted - full.burned,
        }
    average = result["density"]["last_7_full_days_average"]
    if average:
        result["years_to_100m_at_recent_rate"] = round(TARGET_EVENTS / average / 365, 1)


def measure_usdt(
    portal: Portal, alchemy: Alchemy, result: dict, end: int, max_pages: int
) -> None:
    hours = result["hours"]
    stop_time = header(alchemy, hex(end))["timestamp"] // 3600 * 3600
    begin_time = stop_time - hours * 3600
    start, start_check = first_block_at(portal, alchemy, begin_time)
    after, stop_check = first_block_at(portal, alchemy, stop_time)
    stop = after - 1
    blocks = stop - start + 1
    result["range"] = [start, stop]
    result["blocks"] = blocks
    result["seconds_per_block"] = round(hours * 3600 / blocks, 3)
    result["time_to_block_checks"] = [start_check, stop_check]
    label = f"USDT {hours}h"
    alchemy_tally, result["alchemy"] = alchemy_transfers(
        alchemy, USDT, start, stop, label, max_pages
    )
    result["alchemy_totals"] = alchemy_tally.summary()
    full, upto, result["sqd"] = sqd_transfers(
        portal, USDT, start, stop, label, result["alchemy"]["covered_to_block"]
    )
    result["sqd_totals"] = full.summary()
    result["reconcile"] = compare(upto, alchemy_tally)
    result["reconcile"]["through_block"] = result["alchemy"]["covered_to_block"]
    result["events_per_hour"] = round(full.events / hours)


def projections(usdt: dict) -> dict:
    events = usdt["sqd_totals"]["events"]
    alchemy = usdt["alchemy"]
    sqd_rate = usdt["sqd"]["events_per_second"]
    alchemy_rate = alchemy["events_per_second"]
    fetched = usdt["alchemy_totals"]["events"]
    cu_per_event = alchemy["estimated_cu"] / fetched if fetched else None
    result = {
        "basis": "按本次 USDT 实测速度线性外推，公共服务速度会随负载变化",
        "target_events": TARGET_EVENTS,
        "usdt_events_per_day_estimate": round(events / usdt["hours"] * 24),
    }
    if sqd_rate:
        result["sqd_hours_for_target"] = round(TARGET_EVENTS / sqd_rate / 3600, 1)
    if alchemy_rate:
        result["alchemy_hours_for_target"] = round(
            TARGET_EVENTS / alchemy_rate / 3600, 1
        )
    if cu_per_event:
        cu = round(TARGET_EVENTS * cu_per_event)
        result["alchemy_cu_for_target"] = cu
        result["alchemy_free_months_for_target"] = round(cu / FREE_CU_PER_MONTH, 2)
    return result


def summary_lines(report: dict) -> list[str]:
    lines = []
    sources = report.get("sources")
    if sources:
        lines.append(
            f"SQD 数据集 {sources['dataset']}：实时头落后 Alchemy 最新 "
            f"{sources['sqd_head_lag_blocks']} 块，最终确认头落后 "
            f"{sources['sqd_finalized_lag_blocks']} 块；Alchemy finalized 落后 "
            f"{sources['alchemy_finalized_lag_blocks']} 块（约 0.45 秒一块）"
        )
    for item in report.get("bem", []):
        address = item.get("address", "")
        if "reconcile" not in item:
            reason = item.get("note") or item.get("error") or "未完成"
            lines.append(f"BEM {address}：{reason}")
            continue
        match = item["reconcile"]
        same = (
            match["same_count"]
            and match["same_amount_sum"]
            and match["same_fingerprint"]
        )
        lines.append(
            f"BEM {address}：SQD {item['sqd_totals']['events']:,} 条，"
            f"Alchemy {item['alchemy_totals']['events']:,} 条，"
            f"{'一致' if same else '不一致'}；"
            f"近 7 天日均 {item['density']['last_7_full_days_average']}"
        )
    usdt = report.get("usdt")
    if isinstance(usdt, dict) and "error" in usdt:
        lines.append(f"USDT 压力测试：{usdt['error']}")
    elif isinstance(usdt, dict) and "reconcile" in usdt:
        match = usdt["reconcile"]
        same = (
            match["same_count"]
            and match["same_amount_sum"]
            and match["same_fingerprint"]
        )
        lines.append(
            f"USDT {usdt['hours']} 小时：{usdt['sqd_totals']['events']:,} 条，"
            f"SQD {usdt['sqd']['elapsed_seconds']} 秒，"
            f"Alchemy {usdt['alchemy']['elapsed_seconds']} 秒，{'一致' if same else '不一致'}"
        )
    target = report.get("projection_100m")
    if target:
        lines.append(
            f"1 亿条外推：SQD 约 {target.get('sqd_hours_for_target')} 小时，"
            f"Alchemy 约 {target.get('alchemy_hours_for_target')} 小时、"
            f"{target.get('alchemy_cu_for_target')} CU"
        )
    return lines


def run(args, report: dict) -> None:
    settings = load_settings()
    url = settings.require_rpc()
    timeout = httpx.Timeout(120, connect=15)
    with (
        httpx.Client(timeout=timeout, follow_redirects=False) as http,
        RpcClient(url) as rpc,
    ):
        portal = Portal(args.portal, args.dataset, http)
        alchemy = Alchemy(rpc)
        try:
            say("检查数据源、数据集和最终确认高度")
            report["sources"] = check_sources(portal, alchemy, args.dataset)
            end = report["sources"]["measure_end_block"]
            low = report["sources"]["start_block"] or 0
            try:
                report["hash_check"] = compare_hashes(portal, alchemy, [end])
            except ChainScopeError as exc:
                report["hash_check"] = {"error": str(exc)}
            report["bem"] = []
            for token in args.bem:
                item = {"address": token}
                report["bem"].append(item)
                try:
                    measure_token(portal, alchemy, item, low, end, args.max_pages)
                except ChainScopeError as exc:
                    item["error"] = str(exc)
            if args.usdt_hours:
                usdt = {"purpose": "压力测试，只测量，不入库不跟踪"}
                usdt["hours"] = args.usdt_hours
                report["usdt"] = usdt
                try:
                    measure_usdt(portal, alchemy, usdt, end, args.max_pages)
                    report["projection_100m"] = projections(usdt)
                except ChainScopeError as exc:
                    usdt["error"] = str(exc)
        finally:
            report["portal_usage"] = {
                key: round(value, 1) if isinstance(value, float) else value
                for key, value in portal.stats.items()
            }
            report["alchemy_usage"] = alchemy.usage()


def contract_address(value: str) -> str:
    return fixed_hex(value, ADDRESS, "--bem")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="M0：用真实链上数据测量 SQD 与 Alchemy"
    )
    parser.add_argument("--dataset", default="binance-mainnet")
    parser.add_argument("--portal", default="https://portal.sqd.dev")
    parser.add_argument(
        "--bem",
        type=contract_address,
        action="append",
        help="BEM 合约地址，可重复；默认已确认的 BEM 合约",
    )
    parser.add_argument(
        "--usdt-hours",
        type=int,
        default=1,
        choices=range(0, 25),
        metavar="0-24",
        help="USDT 压力测试小时数，0 表示跳过",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=3000,
        help="每个区间 Alchemy Transfers 最多翻页数（每页 120 CU）",
    )
    parser.add_argument(
        "--output", type=Path, help="报告路径，默认 reports/m0-时间.json"
    )
    args = parser.parse_args()
    args.bem = args.bem or list(BEM_CANDIDATES)
    started = datetime.now(UTC)
    output = args.output or ROOT / "reports" / f"m0-{started:%Y%m%d-%H%M%S}.json"
    report: dict = {
        "tool": "measure_sources",
        "read_only": True,
        "started_at_utc": started.isoformat(timespec="seconds"),
    }
    code = 0
    try:
        run(args, report)
    except KeyboardInterrupt:
        report["interrupted"] = True
        say("已按 Ctrl+C 中断，保存已测到的部分")
        code = 130
    except ChainScopeError as exc:
        report["error"] = str(exc)
        code = 1
    except httpx.HTTPError as exc:
        report["error"] = f"网络错误：{type(exc).__name__}"
        code = 1
    report["finished_at_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for line in summary_lines(report):
        print(line)
    if "error" in report:
        print(f"失败：{report['error']}")
    sections = [*report.get("bem", []), report.get("usdt") or {}]
    if code == 0 and ("error" in report or any("error" in s for s in sections)):
        code = 1
    print(f"完整报告：{output}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
