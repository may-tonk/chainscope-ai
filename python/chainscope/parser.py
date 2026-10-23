"""Normalize exact ERC-20 transfer values; never use Alchemy's float value."""

import re
from datetime import UTC, datetime

from .errors import ChainScopeError

UINT256_MAX = 2**256 - 1
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
HASH = re.compile(r"0x[0-9a-fA-F]{64}")


def fixed_hex(value, pattern: re.Pattern, label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ChainScopeError(f"{label}: 十六进制字段格式错误")
    return value.lower()


def quantity(value, label: str, maximum: int = UINT256_MAX) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", value):
        raise ChainScopeError(f"{label}: 缺少有效十六进制整数")
    number = int(value, 16)
    if number > maximum:
        raise ChainScopeError(f"{label}: 超出允许整数范围")
    return number


def transfer_identity(raw: dict, token: str, start: int, end: int) -> dict:
    """Fields Alchemy Transfers carry by itself; no block header is needed."""
    if not isinstance(raw, dict) or raw.get("category") != "erc20":
        raise ChainScopeError("Transfers 返回了非 ERC-20 记录")
    contract = raw.get("rawContract")
    if not isinstance(contract, dict):
        raise ChainScopeError("缺少 rawContract")
    actual_token = fixed_hex(contract.get("address"), ADDRESS, "token_address")
    number = quantity(raw.get("blockNum"), "block_number", 2**64 - 1)
    if actual_token != token or not start <= number <= end:
        raise ChainScopeError("返回记录的合约或区块超出请求范围")
    tx_hash = fixed_hex(raw.get("hash"), HASH, "tx_hash")
    uid = raw.get("uniqueId")
    match = (
        re.fullmatch(re.escape(tx_hash) + r":log:([0-9]+)", uid.lower())
        if isinstance(uid, str)
        else None
    )
    if not match or int(match[1]) > 2**32 - 1:
        raise ChainScopeError("uniqueId 缺少有效日志索引")
    return {
        "block_number": number,
        "tx_hash": tx_hash,
        "log_index": int(match[1]),
        "from_address": fixed_hex(raw.get("from"), ADDRESS, "from_address"),
        "to_address": fixed_hex(raw.get("to"), ADDRESS, "to_address"),
        "amount": quantity(contract.get("value"), "amount_raw"),
    }


def parse_transfer(raw: dict, token: str, start: int, end: int, block: dict) -> dict:
    identity = transfer_identity(raw, token, start, end)
    number = identity["block_number"]
    if quantity(block.get("number"), "block.number") != number:
        raise ChainScopeError("区块头与转账区块不一致")
    timestamp = datetime.fromtimestamp(
        quantity(block.get("timestamp"), "timestamp"), UTC
    )
    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ChainScopeError("Transfers metadata 格式错误")
    if metadata.get("blockTimestamp"):
        try:
            if not isinstance(metadata["blockTimestamp"], str):
                raise TypeError("timestamp must be text")
            supplied = datetime.fromisoformat(
                metadata["blockTimestamp"].replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            raise ChainScopeError("Transfers 时间戳格式错误") from None
        if supplied.tzinfo is None or supplied != timestamp:
            raise ChainScopeError("Transfers 时间戳与区块头不一致")
    decimals = raw["rawContract"].get("decimal")
    return {
        "chain_id": 56,
        "token_address": token,
        "block_number": number,
        "block_hash": fixed_hex(block.get("hash"), HASH, "block_hash"),
        "block_time_utc": timestamp.replace(tzinfo=None),
        "tx_hash": identity["tx_hash"],
        "log_index": identity["log_index"],
        "from_address": identity["from_address"],
        "to_address": identity["to_address"],
        "amount_raw": str(identity["amount"]),
        "token_decimals": None
        if decimals is None
        else quantity(decimals, "decimals", 255),
    }
