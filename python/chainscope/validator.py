"""Compare Transfer events to authoritative transaction receipt logs."""

from .errors import ChainScopeError
from .parser import ADDRESS, HASH, fixed_hex, quantity

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def verify_receipt(event: dict, receipt: dict) -> None:
    if (
        not isinstance(receipt, dict)
        or quantity(receipt.get("status"), "receipt.status") != 1
    ):
        raise ChainScopeError("交易回执不存在或交易未成功")
    if (
        fixed_hex(receipt.get("transactionHash"), HASH, "receipt.tx_hash")
        != event["tx_hash"]
        or fixed_hex(receipt.get("blockHash"), HASH, "receipt.block_hash")
        != event["block_hash"]
        or quantity(receipt.get("blockNumber"), "receipt.block_number")
        != event["block_number"]
    ):
        raise ChainScopeError("回执所属交易或区块不一致")
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        raise ChainScopeError("回执 logs 格式错误")
    if any(not isinstance(log, dict) for log in logs):
        raise ChainScopeError("回执日志不是对象")
    candidates = [
        log
        for log in logs
        if quantity(log.get("logIndex"), "log_index") == event["log_index"]
    ]
    if len(candidates) != 1:
        raise ChainScopeError("交易回执中没有唯一对应日志")
    log = candidates[0]
    topics = log.get("topics")
    if (
        log.get("removed", False)
        or fixed_hex(log.get("transactionHash"), HASH, "log.tx_hash")
        != event["tx_hash"]
        or fixed_hex(log.get("blockHash"), HASH, "log.block_hash")
        != event["block_hash"]
        or quantity(log.get("blockNumber"), "log.block_number") != event["block_number"]
        or fixed_hex(log.get("address"), ADDRESS, "log.address")
        != event["token_address"]
        or not isinstance(topics, list)
        or len(topics) != 3
        or fixed_hex(topics[0], HASH, "topic0") != TRANSFER_TOPIC
    ):
        raise ChainScopeError("回执日志与标准 Transfer 不一致")
    for topic, address in zip(
        topics[1:], (event["from_address"], event["to_address"]), strict=True
    ):
        value = fixed_hex(topic, HASH, "indexed address")
        if value != "0x" + "0" * 24 + address[2:]:
            raise ChainScopeError("回执日志的转出或转入地址不一致")
    data = fixed_hex(log.get("data"), HASH, "Transfer.data")
    if str(quantity(data, "Transfer.value")) != event["amount_raw"]:
        raise ChainScopeError("回执日志与采集金额不一致")
