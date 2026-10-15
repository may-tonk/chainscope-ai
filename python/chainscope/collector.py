"""Alchemy Transfers: debug capture with raw evidence, and per-window reconciliation."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .archive import DEFAULT_BATCH_LIMIT, TOTAL_ARCHIVE_LIMIT, JsonArchive
from .config import ROOT, Settings
from .errors import ChainScopeError
from .parser import (
    ADDRESS,
    HASH,
    fixed_hex,
    parse_transfer,
    quantity,
    transfer_identity,
)
from .rpc_client import RpcClient
from .validator import Tally, verify_receipt

TRANSFERS_CU = 120  # Alchemy compute units per alchemy_getAssetTransfers call


def transfer_params(token: str, start: int, end: int) -> dict:
    return {
        "fromBlock": hex(start),
        "toBlock": hex(end),
        "contractAddresses": [token],
        "category": ["erc20"],
        "excludeZeroValue": False,
        "withMetadata": True,
        "maxCount": "0x3e8",
        "order": "asc",
    }


def next_page_key(result: dict, keys: set) -> str | None:
    key = result.get("pageKey")
    if key in (None, ""):
        return None
    if not isinstance(key, str) or key in keys:
        raise ChainScopeError("Transfers 分页游标无效或重复")
    keys.add(key)
    return key


def transfer_identities(
    rpc: RpcClient, token: str, start: int, end: int, *, max_pages: int = 100
) -> tuple[dict, int]:
    """Alchemy's events for one block window, keyed by (tx_hash, log_index)."""
    params = transfer_params(token, start, end)
    seen: dict = {}
    keys: set = set()
    for page in range(1, max_pages + 1):
        result = rpc.call("alchemy_getAssetTransfers", [params.copy()])
        if not isinstance(result, dict) or not isinstance(
            result.get("transfers"), list
        ):
            raise ChainScopeError("Transfers 响应缺少 transfers 列表")
        for raw in result["transfers"]:
            event = transfer_identity(raw, token, start, end)
            key = (event["tx_hash"], event["log_index"])
            if key in seen and seen[key] != event:
                raise ChainScopeError("分页返回的同一事件内容冲突")
            seen[key] = event
        key = next_page_key(result, keys)
        if key is None:
            return seen, page
        params["pageKey"] = key
    raise ChainScopeError("对账达到分页上限但仍有下一页；请缩小窗口")


def transfer_totals(
    rpc: RpcClient, token: str, start: int, end: int, *, max_pages: int = 100
) -> tuple[Tally, int]:
    """Alchemy's own count, amount sum and fingerprint for one block window."""
    seen, pages = transfer_identities(rpc, token, start, end, max_pages=max_pages)
    tally = Tally()
    for event in seen.values():
        tally.add(
            event["tx_hash"],
            event["log_index"],
            event["from_address"],
            event["to_address"],
            event["amount"],
        )
    return tally, pages


def fetch_debug(
    settings: Settings,
    token: str,
    start: int,
    end: int,
    *,
    max_pages: int,
    receipt_limit: int,
    archive_max_bytes: int = DEFAULT_BATCH_LIMIT,
) -> dict:
    """Capture a small range into raw/ for debugging; nothing is written to MySQL."""
    token = fixed_hex(token, ADDRESS, "token")
    if not 0 < archive_max_bytes <= TOTAL_ARCHIVE_LIMIT:
        raise ChainScopeError("归档容量不合法")
    batch_id = str(uuid4())
    directory = ROOT / "raw" / batch_id
    with RpcClient(settings.require_rpc()) as rpc:
        _, manifest = capture(
            rpc,
            token,
            start,
            end,
            directory,
            batch_id=batch_id,
            max_pages=max_pages,
            receipt_limit=receipt_limit,
            archive_max_bytes=archive_max_bytes,
        )
    return {**manifest, "raw_directory": str(directory)}


def capture(
    rpc: RpcClient,
    token: str,
    start: int,
    end: int,
    directory: Path | None = None,
    *,
    batch_id: str | None = None,
    max_pages: int = 20,
    receipt_limit: int = 10,
    archive_max_bytes: int = DEFAULT_BATCH_LIMIT,
) -> tuple[list[dict], dict]:
    token = fixed_hex(token, ADDRESS, "token")
    if (
        not 0 <= start <= end < 2**64
        or not 1 <= max_pages <= 100
        or not 1 <= receipt_limit <= 100
    ):
        raise ChainScopeError("区块范围、分页上限或回执核验上限不合法")
    if batch_id is None:
        if directory is None:
            raise ChainScopeError("采集任务缺少批次 ID")
        batch_id = directory.name
    archive = (
        JsonArchive(directory, batch_limit=archive_max_bytes)
        if directory is not None
        else None
    )
    rpc.archive = archive
    manifest = {
        "batch_id": batch_id,
        "provider": "alchemy",
        "method": "alchemy_getAssetTransfers",
        "chain_id": 56,
        "token_address": token,
        "start_block": start,
        "end_block": end,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "status": "running",
        "max_pages": max_pages,
        "receipt_limit": receipt_limit,
        "archive_enabled": archive is not None,
    }
    if archive is not None:
        archive.write_json("manifest.json", manifest)
    try:
        events, details = _capture(rpc, token, start, end, max_pages, receipt_limit)
    except (ChainScopeError, OSError, ValueError, TypeError, KeyError) as exc:
        manifest.update(
            status="failed", error_type=type(exc).__name__, rpc_requests=rpc.requests
        )
        if archive is not None:
            try:
                archive.write_json("manifest.json", manifest)
            except (ChainScopeError, OSError):
                # Preserve the original failure, including quota or disk failures.
                pass
        raise
    manifest.update(
        details,
        status="verified_not_persisted",
        finished_at_utc=datetime.now(UTC).isoformat(),
        rpc_requests=rpc.requests,
    )
    if archive is not None:
        archive.write_json("manifest.json", manifest)
    return events, manifest


def _capture(rpc, token, start, end, max_pages, receipt_limit):
    if quantity(rpc.call("eth_chainId", []), "chain_id") != 56:
        raise ChainScopeError("RPC 不是 BSC 主网（chain_id=56）")
    head = quantity(rpc.call("eth_blockNumber", []), "head")
    if end > head - 12:
        raise ChainScopeError("结束区块需要至少 12 个后续区块，避免采集未稳定的链头")
    code = rpc.call("eth_getCode", [token, hex(end)])
    if not isinstance(code, str) or code in ("0x", "0x0"):
        raise ChainScopeError("指定地址在结束区块没有合约代码")
    anchor = rpc.call("eth_getBlockByNumber", [hex(end), False])
    if (
        not isinstance(anchor, dict)
        or quantity(anchor.get("number"), "anchor.number") != end
    ):
        raise ChainScopeError("结束区块不存在或区块头不匹配")
    anchor_hash = fixed_hex(anchor.get("hash"), HASH, "end_block_hash")
    blocks = {end: anchor}
    params = transfer_params(token, start, end)
    keys: set = set()
    events = {}
    raw_count = 0
    for page in range(1, max_pages + 1):
        result = rpc.call("alchemy_getAssetTransfers", [params.copy()])
        if not isinstance(result, dict) or not isinstance(
            result.get("transfers"), list
        ):
            raise ChainScopeError("Transfers 响应缺少 transfers 列表")
        for raw in result["transfers"]:
            if not isinstance(raw, dict):
                raise ChainScopeError("Transfers 列表包含非对象记录")
            number = quantity(raw.get("blockNum"), "block_number", 2**64 - 1)
            if not start <= number <= end:
                raise ChainScopeError("采集记录区块超出请求范围")
            if number not in blocks:
                blocks[number] = rpc.call("eth_getBlockByNumber", [hex(number), False])
            if not isinstance(blocks[number], dict):
                raise ChainScopeError("转账对应区块不存在")
            event = parse_transfer(raw, token, start, end, blocks[number])
            key = (event["tx_hash"], event["log_index"])
            if key in events and events[key] != event:
                raise ChainScopeError("分页返回的同一事件内容冲突")
            events[key] = event
            raw_count += 1
        key = next_page_key(result, keys)
        if key is None:
            break
        params["pageKey"] = key
    else:
        raise ChainScopeError("达到分页上限但仍有下一页，批次未完成；请缩小区块范围")
    ordered = sorted(
        events.values(), key=lambda event: (event["block_number"], event["log_index"])
    )
    receipts = {}
    verified = 0
    for event in ordered:
        tx = event["tx_hash"]
        if tx not in receipts and len(receipts) < receipt_limit:
            receipts[tx] = rpc.call("eth_getTransactionReceipt", [tx])
        if tx in receipts:
            verify_receipt(event, receipts[tx])
            verified += 1
    after = rpc.call("eth_getBlockByNumber", [hex(end), False])
    if (
        not isinstance(after, dict)
        or fixed_hex(after.get("hash"), HASH, "anchor.after") != anchor_hash
    ):
        raise ChainScopeError("采集期间结束区块发生变化，请重新采集")
    return ordered, {
        "end_block_hash": anchor_hash,
        "pages": page,
        "raw_count": raw_count,
        "event_count": len(ordered),
        "source_duplicate_count": raw_count - len(ordered),
        "receipt_transactions": len(receipts),
        "receipt_verified_events": verified,
        "receipt_validation_scope": "all_events"
        if verified == len(ordered)
        else "sampled_transactions",
        "completeness_scope": "provider pagination exhausted; receipts check returned events, not missing events",
    }
