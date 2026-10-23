"""Second-source checks for a collected window: Alchemy totals and receipts."""

import random

from .collector import transfer_identities
from .dex import SWAP_TOPICS, swap_core
from .errors import ChainScopeError, ReconcileMismatch
from .parser import ADDRESS, HASH, fixed_hex, quantity
from .rpc_client import RpcClient
from .validator import verify_receipt

FIELDS = ("block_number", "from_address", "to_address", "amount")
RECEIPTS_PER_WINDOW = 3
MAX_DIFFS = 1000


def compare_sources(primary: list[dict], secondary: dict) -> list[dict]:
    """Event-level differences between Portal events and Alchemy identities."""
    diffs = []
    ours = {(e["tx_hash"], e["log_index"]): e for e in primary}
    for key, event in ours.items():
        other = secondary.get(key)
        if other is None:
            diffs.append({"kind": "missing_in_alchemy", "event": event})
            continue
        changed = {
            f: {"sqd": str(event[f]), "alchemy": str(other[f])}
            for f in FIELDS
            if event[f] != other[f]
        }
        if changed:
            diffs.append({"kind": "field_conflict", "event": event, "fields": changed})
    for key, other in secondary.items():
        if key not in ours:
            diffs.append({"kind": "missing_in_sqd", "event": other})
    return [
        {
            "kind": d["kind"],
            "tx_hash": d["event"]["tx_hash"],
            "log_index": d["event"]["log_index"],
            "detail": {
                "block_number": d["event"]["block_number"],
                **({"fields": d["fields"]} if "fields" in d else {}),
            },
        }
        for d in diffs[:MAX_DIFFS]
    ]


def reconcile_window(rpc: RpcClient, token: str, start: int, end: int, events):
    """Return Alchemy's differences from the Portal events of one window."""
    secondary, _ = transfer_identities(rpc, token, start, end)
    return compare_sources(events, secondary)


def finalized_block(rpc: RpcClient) -> int:
    block = rpc.call("eth_getBlockByNumber", ["finalized", False])
    if not isinstance(block, dict):
        raise ChainScopeError("RPC 不支持 finalized 区块标签")
    return quantity(block.get("number"), "finalized.number")


def choose(keys, seed: int, count: int) -> list:
    keys = sorted(keys)
    if len(keys) > count:
        keys = sorted(random.Random(seed).sample(keys, count))
    return keys


def missing(receipt, tx_hash: str) -> None:
    """No receipt yet is "cannot verify now", not a mismatch: the window is retried."""
    if receipt is None:
        raise ChainScopeError(f"RPC 暂时取不到交易 {tx_hash[:12]}… 的回执，稍后重试")


def sample_receipts(
    rpc: RpcClient, rows: list[dict], seed: int, count: int = RECEIPTS_PER_WINDOW
) -> int:
    """Verify every event of a few randomly chosen transactions against receipts.

    The choice is seeded by the window start, so a rerun checks the same ones.
    A receipt that disagrees raises ReconcileMismatch; RPC failures propagate
    as ordinary errors.
    """
    by_tx: dict[str, list[dict]] = {}
    for row in rows:
        by_tx.setdefault(row["tx_hash"], []).append(row)
    verified = 0
    for tx_hash in choose(by_tx, seed, count):
        receipt = rpc.call(
            "eth_getTransactionReceipt", [fixed_hex(tx_hash, HASH, "tx")]
        )
        missing(receipt, tx_hash)
        for event in by_tx[tx_hash]:
            try:
                verify_receipt(event, receipt)
            except ChainScopeError as exc:
                raise ReconcileMismatch(
                    f"回执核对失败：{exc}",
                    [
                        {
                            "kind": "receipt_mismatch",
                            "tx_hash": tx_hash,
                            "log_index": event["log_index"],
                            "detail": {"reason": str(exc)},
                        }
                    ],
                ) from None
            verified += 1
    return verified


def sample_swap_receipts(
    rpc: RpcClient,
    swap_rows: list[dict],
    pools: dict,
    seed: int,
    count: int = RECEIPTS_PER_WINDOW,
) -> int:
    """Check a few swap transactions against their receipts, an independent source.

    Every Swap log of a tracked pool in the receipt must be in the collected
    set with the same side and amounts, and the sender must match.
    """
    by_tx: dict[str, list[dict]] = {}
    for row in swap_rows:
        by_tx.setdefault(row["tx_hash"], []).append(row)
    verified = 0
    for tx_hash in choose(by_tx, seed, count):
        receipt = rpc.call("eth_getTransactionReceipt", [tx_hash])
        missing(receipt, tx_hash)
        collected = {s["log_index"]: s for s in by_tx[tx_hash]}
        problems = []
        if not isinstance(receipt, dict) or not isinstance(receipt.get("logs"), list):
            problems.append("回执缺失或格式错误")
        else:
            if (
                fixed_hex(receipt.get("from"), ADDRESS, "receipt.from")
                != next(iter(collected.values()))["tx_from"]
            ):
                problems.append("交易发起人不一致")
            seen = set()
            for log in receipt["logs"]:
                address = str(log.get("address")).lower()
                topics = log.get("topics") or []
                if address not in pools or not topics or topics[0] not in SWAP_TOPICS:
                    continue
                index = quantity(log.get("logIndex"), "logIndex")
                core = swap_core(log, pools[address])
                if core is None:
                    continue
                seen.add(index)
                ours = collected.get(index)
                if ours is None:
                    problems.append(f"日志 {index} 的 Swap 未被采集")
                elif any(ours[k] != v for k, v in core.items()) or (
                    ours["pool_address"] != address
                ):
                    problems.append(f"日志 {index} 的方向或数量不一致")
            for index in collected.keys() - seen:
                problems.append(f"采集的日志 {index} 不在回执中")
        if problems:
            raise ReconcileMismatch(
                f"Swap 回执核对失败：{'；'.join(problems)}",
                [
                    {
                        "kind": "swap_receipt_mismatch",
                        "tx_hash": tx_hash,
                        "detail": {"problems": problems},
                    }
                ],
            )
        verified += len(collected)
    return verified
