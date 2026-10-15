"""Second-source checks for a collected window: Alchemy totals and receipts."""

import random

from .collector import transfer_identities
from .errors import ChainScopeError
from .parser import HASH, fixed_hex, quantity
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


def sample_receipts(
    rpc: RpcClient, rows: list[dict], seed: int, count: int = RECEIPTS_PER_WINDOW
) -> int:
    """Verify every event of a few randomly chosen transactions against receipts.

    The choice is seeded by the window start, so a rerun checks the same ones.
    """
    by_tx: dict[str, list[dict]] = {}
    for row in rows:
        by_tx.setdefault(row["tx_hash"], []).append(row)
    chosen = sorted(by_tx)
    if len(chosen) > count:
        chosen = sorted(random.Random(seed).sample(chosen, count))
    verified = 0
    for tx_hash in chosen:
        receipt = rpc.call(
            "eth_getTransactionReceipt", [fixed_hex(tx_hash, HASH, "tx")]
        )
        for event in by_tx[tx_hash]:
            verify_receipt(event, receipt)
            verified += 1
    return verified
