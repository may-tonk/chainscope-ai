"""DEX pools of the tracked token and their Swap logs: discovery, parsing, sides.

A Swap is classified from the pool's point of view: the pool paying out the
token is a buy, the pool receiving it is a sell. The trader is the transaction
sender; same-transaction token Transfers tell whether that attribution is
direct, routed through another contract, or ambiguous (aggregators, multi-hop).
"""

from collections import defaultdict
from datetime import UTC, datetime

import sqlalchemy as sa

from .database import utcnow
from .errors import ChainScopeError
from .jobs import reset_windows
from .models import pools, swaps
from .parser import ADDRESS, HASH, fixed_hex, quantity

PAIR_CREATED = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
POOL_CREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
SWAP_V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
SWAP_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
# PancakeSwap V3 adds protocolFeesToken0/1 to the event, so its topic differs.
SWAP_PANCAKE_V3 = "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83"
SWAP_TOPICS = (SWAP_V2, SWAP_V3, SWAP_PANCAKE_V3)
FACTORIES = {
    "pancake_v2": ("0xca143ce32fe78f1f7019d7d551a6402fc5350c73", PAIR_CREATED),
    "pancake_v3": ("0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865", POOL_CREATED),
}
# All four use 18 decimals on BSC.
KNOWN_QUOTES = {
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": "WBNB",
    "0x55d398326f99059ff775485246999027b3197955": "USDT",
    "0xe9e7cea3dedca5984780bafc599bd69add087d56": "BUSD",
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": "USDC",
}
TOKEN0, TOKEN1, FEE, GET_RESERVES = (
    "0x0dfe1681",
    "0xd21220a7",
    "0xddca3f43",
    "0x0902f1ac",
)
INT256 = 2**255


def word(data: str, index: int) -> int:
    start = 2 + 64 * index
    chunk = data[start : start + 64]
    if len(chunk) != 64:
        raise ChainScopeError("日志 data 长度不足")
    return int(chunk, 16)


def signed(value: int) -> int:
    return value - 2**256 if value >= INT256 else value


def topic_address(topic: str) -> str:
    return "0x" + fixed_hex(topic, HASH, "topic")[-40:]


def padded(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def discovery_query(token: str) -> dict:
    """Factory creation logs where the token is token0 or token1."""
    logs = []
    for factory, topic in FACTORIES.values():
        logs.append(
            {"address": [factory], "topic0": [topic], "topic1": [padded(token)]}
        )
        logs.append(
            {"address": [factory], "topic0": [topic], "topic2": [padded(token)]}
        )
    return {
        "type": "evm",
        "fields": {
            "block": {"number": True},
            "log": {"address": True, "topics": True, "data": True},
        },
        "logs": logs,
    }


def pool_row(token: str, pool: str, dex: str, token0: str, token1: str) -> dict:
    if token not in (token0, token1):
        raise ChainScopeError(f"池子 {pool} 不包含代币 {token}")
    quote = token1 if token0 == token else token0
    return {
        "pool_address": pool,
        "dex": dex,
        "token0": token0,
        "token1": token1,
        "token_is_token0": token0 == token,
        "quote_token": quote,
        "quote_symbol": KNOWN_QUOTES.get(quote),
    }


def created_pools(block: dict, token: str) -> list[dict]:
    pools = []
    by_factory = {f: name for name, (f, _) in FACTORIES.items()}
    for log in block.get("logs") or []:
        dex = by_factory.get(fixed_hex(log.get("address"), ADDRESS, "factory"))
        topics = log.get("topics") or []
        if dex is None or len(topics) < 3:
            continue
        token0, token1 = topic_address(topics[1]), topic_address(topics[2])
        if token not in (token0, token1):
            continue
        data = log.get("data") or ""
        # PairCreated(token0, token1, pair, n); PoolCreated(.., tickSpacing, pool)
        pool = "0x" + format(word(data, 0 if dex == "pancake_v2" else 1), "064x")[-40:]
        pools.append(
            {
                **pool_row(token, pool, dex, token0, token1),
                "fee": int(topics[3], 16) if len(topics) > 3 else None,
                "created_block": block["header"]["number"],
            }
        )
    return pools


def swap_log_filter(pools) -> list[dict]:
    if not pools:
        return []
    return [
        {"address": sorted(pools), "topic0": list(SWAP_TOPICS), "transaction": True}
    ]


def block_swaps(block: dict, pools: dict) -> list[dict]:
    """Swap legs of the tracked pools in one Portal block, with the tx sender."""
    header = block["header"]
    senders = {}
    for tx in block.get("transactions") or []:
        if isinstance(tx, dict) and tx.get("hash") and tx.get("from"):
            senders[fixed_hex(tx["hash"], HASH, "tx.hash")] = fixed_hex(
                tx["from"], ADDRESS, "tx.from"
            )
    swaps = []
    for log in block.get("logs") or []:
        address = str(log.get("address")).lower()
        pool = pools.get(address)
        topics = log.get("topics") or []
        if pool is None:
            continue
        if not topics or topics[0] not in SWAP_TOPICS:
            raise ChainScopeError("Portal 返回了池子的非 Swap 日志")
        data = log.get("data") or ""
        if topics[0] == SWAP_V2:
            in0, in1, out0, out1 = (word(data, i) for i in range(4))
            delta0, delta1 = in0 - out0, in1 - out1
        else:
            delta0, delta1 = signed(word(data, 0)), signed(word(data, 1))
        token_delta, quote_delta = (
            (delta0, delta1) if pool["token_is_token0"] else (delta1, delta0)
        )
        if token_delta == 0:
            continue
        tx_hash = fixed_hex(log.get("transactionHash"), HASH, "tx_hash")
        if tx_hash not in senders:
            raise ChainScopeError("Swap 日志缺少对应交易的发起人")
        swaps.append(
            {
                "chain_id": 56,
                "pool_address": address,
                "block_number": header["number"],
                "block_time_utc": datetime.fromtimestamp(
                    header["timestamp"], UTC
                ).replace(tzinfo=None),
                "tx_hash": tx_hash,
                "log_index": log["logIndex"],
                "tx_from": senders[tx_hash],
                # Pool-side deltas: the pool paying out the token is a buy.
                "side": "buy" if token_delta < 0 else "sell",
                "token_amount_raw": str(abs(token_delta)),
                "quote_amount_raw": str(abs(quote_delta)),
                "quote_token": pool["quote_token"],
            }
        )
    return swaps


def attribute(swap: dict, transfers: list[dict]) -> dict:
    """Trader and how confidently the swap belongs to them.

    transfers: token Transfers of the same transaction.
    direct    - the pool paid the sender (buy) or the sender paid the pool (sell)
    router    - the sender received (buy) or sent (sell) the token elsewhere in the tx
    ambiguous - neither; e.g. an aggregator delivering to a third party
    """
    pool, sender = swap["pool_address"], swap["tx_from"]
    if swap["side"] == "buy":
        legs = [t for t in transfers if t["from_address"] == pool]
        direct = any(t["to_address"] == sender for t in legs)
        routed = any(t["to_address"] == sender for t in transfers)
    else:
        legs = [t for t in transfers if t["to_address"] == pool]
        direct = any(t["from_address"] == sender for t in legs)
        routed = any(t["from_address"] == sender for t in transfers)
    amount = int(swap["token_amount_raw"])
    amounts = [int(t["amount"]) for t in legs]
    return {
        **swap,
        "trader": sender,
        "attribution": "direct" if direct else "router" if routed else "ambiguous",
        # False for fee-on-transfer tokens: the pool moved a different amount.
        "transfer_matched": amount in amounts or sum(amounts) == amount,
    }


def attribute_all(swap_legs: list[dict], transfers: list[dict]) -> list[dict]:
    by_tx = defaultdict(list)
    for t in transfers:
        by_tx[t["tx_hash"]].append(t)
    return [attribute(s, by_tx[s["tx_hash"]]) for s in swap_legs]


# Pool registry -------------------------------------------------------------


def load_pools(connection, job_id: str, *, enabled_only=True) -> dict:
    query = sa.select(pools).where(pools.c.job_id == job_id)
    if enabled_only:
        query = query.where(pools.c.enabled.is_(True))
    return {
        row["pool_address"]: dict(row)
        for row in connection.execute(query.order_by(pools.c.created_block)).mappings()
    }


def save_pools(
    engine, job_id: str, found: list[dict], found_by: str, *, enable_known=True
) -> list[dict]:
    """Insert pools not yet known. Pools quoted in a known token start enabled."""
    added = []
    with engine.begin() as connection:
        known = load_pools(connection, job_id, enabled_only=False)
        for pool in found:
            if pool["pool_address"] in known:
                continue
            row = {
                **pool,
                "job_id": job_id,
                "enabled": enable_known and pool["quote_symbol"] is not None,
                "found_by": found_by,
                "added_at_utc": utcnow(),
            }
            connection.execute(pools.insert().values(**row))
            known[pool["pool_address"]] = row
            added.append(row)
    return added


def discover_pools(engine, portal, job: dict, head: int) -> list[dict]:
    """PancakeSwap V2/V3 pools of the token created from the job start to head."""
    token = job["token_address"]
    found = []
    for block in portal.stream(discovery_query(token), job["start_block"], head):
        found.extend(created_pools(block, token))
    return save_pools(engine, job["job_id"], found, "factory")


def call_address(rpc, pool: str, selector: str) -> str:
    raw = rpc.call("eth_call", [{"to": pool, "data": selector}, "latest"])
    if not isinstance(raw, str) or len(raw) != 66:
        raise ChainScopeError(f"池子 {pool} 的 {selector} 返回格式错误")
    return topic_address(raw)


def inspect_pool(rpc, token: str, pool: str) -> dict:
    """Read token0/token1 (and fee for V3) of a manually given pool."""
    pool = fixed_hex(pool, ADDRESS, "pool")
    token0 = call_address(rpc, pool, TOKEN0)
    token1 = call_address(rpc, pool, TOKEN1)
    try:
        fee = quantity(
            rpc.call("eth_call", [{"to": pool, "data": FEE}, "latest"]), "fee", 2**24
        )
        dex = "v3_manual"
    except ChainScopeError:
        fee, dex = None, "v2_manual"
    return {**pool_row(token, pool, dex, token0, token1), "fee": fee}


def set_pool_enabled(engine, job_id: str, pool: str, enabled: bool) -> dict:
    """Enable: redo windows from the pool's creation. Disable: drop its swaps."""
    with engine.begin() as connection:
        row = connection.execute(
            sa.select(pools.c.created_block).where(
                pools.c.job_id == job_id, pools.c.pool_address == pool
            )
        ).first()
        if row is None:
            raise ChainScopeError(
                f"任务 {job_id} 没有池子 {pool}；先 pools discover 或 pools add"
            )
        connection.execute(
            pools.update()
            .where(pools.c.job_id == job_id, pools.c.pool_address == pool)
            .values(enabled=enabled)
        )
        removed = 0
        if not enabled:
            removed = connection.execute(
                swaps.delete().where(
                    swaps.c.job_id == job_id, swaps.c.pool_address == pool
                )
            ).rowcount
    reset = reset_windows(engine, job_id, first=row.created_block) if enabled else 0
    return {
        "pool": pool,
        "enabled": enabled,
        "windows_reset": reset,
        "swaps_removed": removed,
    }


def reserves(rpc, pool: dict, block: int) -> tuple[int, int]:
    """(token reserve, quote reserve) of a V2 pool at a block."""
    raw = rpc.call(
        "eth_call", [{"to": pool["pool_address"], "data": GET_RESERVES}, hex(block)]
    )
    if not isinstance(raw, str) or len(raw) != 2 + 64 * 3:
        raise ChainScopeError("getReserves 返回格式错误")
    r0, r1 = word(raw, 0), word(raw, 1)
    return (r0, r1) if pool["token_is_token0"] else (r1, r0)
