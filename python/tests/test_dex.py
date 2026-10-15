"""Pools, swaps, balances, holders and labels on a small synthetic chain."""

import json

import httpx
import pytest
import sqlalchemy as sa
from conftest import requires_mysql
from test_jobs import TOKEN, count, new_job

from chainscope.dex import (
    FACTORIES,
    PAIR_CREATED,
    POOL_CREATED,
    SWAP_PANCAKE_V3,
    SWAP_V2,
    attribute,
    block_swaps,
    created_pools,
    load_pools,
    set_pool_enabled,
)
from chainscope.errors import ChainScopeError
from chainscope.holders import (
    import_labels,
    refresh_balances,
    top_holders,
    verify_holders,
)
from chainscope.jobs import job_status
from chainscope.models import jobs, swaps, transfers, windows
from chainscope.query import swap_summary
from chainscope.rpc_client import RpcClient
from chainscope.sqd import Portal, block_transfers, transfer_query
from chainscope.sync import pools_discover, redo_job, sync_job
from chainscope.validator import TRANSFER_TOPIC

ZERO = "0x" + "0" * 40
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
FAKE = "0x" + "9" * 40  # a quote token nobody vouches for
POOL = "0x" + "a" * 40
JUNK = "0x" + "b" * 40
ALICE, BOB, ROUTER, CAROL = ("0x" + c * 40 for c in "4567")


def word(n: int) -> str:
    return format(n % 2**256, "064x")


def topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def tx(n: int) -> str:
    return "0x" + format(n, "064x")


def transfer(sender, receiver, amount, tx_hash):
    return {
        "address": TOKEN,
        "topics": [TRANSFER_TOPIC, topic(sender), topic(receiver)],
        "data": "0x" + word(amount),
        "transactionHash": tx_hash,
    }


def swap_v2(pool, in0, in1, out0, out1, tx_hash):
    return {
        "address": pool,
        "topics": [SWAP_V2, topic(ROUTER), topic(ROUTER)],
        "data": "0x" + "".join(word(v) for v in (in0, in1, out0, out1)),
        "transactionHash": tx_hash,
    }


def block(number, txs):
    """txs: [(sender, [log, ...]), ...]; log indexes follow block order."""
    logs, senders, index = [], [], 0
    for sender, entries in txs:
        for entry in entries:
            logs.append({**entry, "logIndex": index})
            index += 1
        senders.append({"hash": entries[0]["transactionHash"], "from": sender})
    return {
        "header": {
            "number": number,
            "hash": "0x" + format(number, "064x"),
            "timestamp": 1_700_000_000 + number,
        },
        "logs": logs,
        "transactions": senders,
    }


def factory_log(name, token0, token1, pool, fee=None):
    address, event = FACTORIES[name]
    topics = [event, topic(token0), topic(token1)]
    if fee is not None:
        topics.append("0x" + word(fee))
        data = "0x" + word(10) + word(int(pool, 16))  # tickSpacing, pool
    else:
        data = "0x" + word(int(pool, 16)) + word(1)  # pair, pair count
    return {"address": address, "topics": topics, "data": data}


# Pool POOL is BEM(token0)/WBNB(token1); amounts below are raw units.
CHAIN = {
    11: block(11, [(ALICE, [transfer(ZERO, ALICE, 1000, tx(11))])]),
    12: block(12, [(ALICE, [transfer(ALICE, POOL, 400, tx(12))])]),
    # Bob buys 100 directly from the pool for 5 WBNB.
    13: block(
        13,
        [
            (
                BOB,
                [transfer(POOL, BOB, 100, tx(13)), swap_v2(POOL, 0, 5, 100, 0, tx(13))],
            )
        ],
    ),
    # Alice sells 50 for 2 WBNB.
    17: block(
        17,
        [
            (
                ALICE,
                [transfer(ALICE, POOL, 50, tx(17)), swap_v2(POOL, 50, 0, 0, 2, tx(17))],
            )
        ],
    ),
    # Bob buys 30 through a router that forwards the tokens to him.
    18: block(
        18,
        [
            (
                BOB,
                [
                    transfer(POOL, ROUTER, 30, tx(18)),
                    swap_v2(POOL, 0, 1, 30, 0, tx(18)),
                    transfer(ROUTER, BOB, 30, tx(18)),
                ],
            )
        ],
    ),
    # Carol pays for a swap that delivers to Alice: an aggregator-style trade.
    22: block(
        22,
        [
            (
                CAROL,
                [
                    transfer(POOL, ROUTER, 10, tx(22)),
                    swap_v2(POOL, 0, 1, 10, 0, tx(22)),
                    transfer(ROUTER, ALICE, 10, tx(22)),
                ],
            )
        ],
    ),
}
FACTORY_BLOCK = {
    "header": {"number": 10},
    "logs": [
        factory_log("pancake_v2", TOKEN, WBNB, POOL),
        factory_log("pancake_v3", FAKE, TOKEN, JUNK, fee=2500),
    ],
}


class SwapChain:
    """Portal stub that filters logs by the requested addresses, like the real one."""

    def __init__(self, head=24):
        self.head = head
        self.queries = []

    def handler(self, request):
        if request.url.path.endswith("/finalized-head"):
            return httpx.Response(200, json={"number": self.head})
        body = json.loads(request.content)
        self.queries.append(body)
        wanted = {a for f in body["logs"] for a in f["address"]}
        if FACTORIES["pancake_v2"][0] in wanted:
            blocks = (
                [FACTORY_BLOCK] if body["fromBlock"] <= 10 <= body["toBlock"] else []
            )
        else:
            with_tx = any(f.get("transaction") for f in body["logs"])
            blocks = []
            for number in sorted(CHAIN):
                if not body["fromBlock"] <= number <= body["toBlock"]:
                    continue
                source = CHAIN[number]
                logs = [log for log in source["logs"] if log["address"] in wanted]
                if logs:
                    blocks.append(
                        {
                            "header": source["header"],
                            "logs": logs,
                            **(
                                {"transactions": source["transactions"]}
                                if with_tx
                                else {}
                            ),
                        }
                    )
        content = b"".join(json.dumps(b).encode() + b"\n" for b in blocks)
        return httpx.Response(200, content=content)

    def portal(self):
        return Portal(
            "https://portal.invalid",
            "binance-mainnet",
            httpx.Client(transport=httpx.MockTransport(self.handler)),
            sleep=lambda _: None,
            min_interval=0,
        )


POOL_INFO = {
    "pool_address": POOL,
    "dex": "pancake_v2",
    "token_is_token0": True,
    "quote_token": WBNB,
}


def test_factory_events_give_pools_and_their_quote_tokens():
    found = created_pools(FACTORY_BLOCK, TOKEN)
    assert [
        (p["pool_address"], p["dex"], p["quote_symbol"], p["fee"]) for p in found
    ] == [
        (POOL, "pancake_v2", "WBNB", None),
        (JUNK, "pancake_v3", None, 2500),
    ]
    assert found[0]["token_is_token0"] and not found[1]["token_is_token0"]
    assert PAIR_CREATED != POOL_CREATED


def test_swap_sides_follow_the_pool_token_delta():
    legs = block_swaps(CHAIN[13], {POOL: POOL_INFO}) + block_swaps(
        CHAIN[17], {POOL: POOL_INFO}
    )
    assert [
        (s["side"], s["token_amount_raw"], s["quote_amount_raw"]) for s in legs
    ] == [
        ("buy", "100", "5"),
        ("sell", "50", "2"),
    ]
    # Same log read with the token as token1 flips which amount is the token.
    flipped = block_swaps(CHAIN[13], {POOL: {**POOL_INFO, "token_is_token0": False}})
    assert (flipped[0]["side"], flipped[0]["token_amount_raw"]) == ("sell", "5")


def test_v3_swaps_use_signed_amounts():
    pool = {**POOL_INFO, "dex": "pancake_v3"}
    log = {
        "address": POOL,
        "topics": [SWAP_PANCAKE_V3, topic(ROUTER), topic(BOB)],
        # amount0 = -70 (pool paid out the token), amount1 = +3, then price data.
        "data": "0x" + word(-70) + word(3) + word(1) * 5,
        "transactionHash": tx(99),
        "logIndex": 0,
    }
    found = block_swaps(
        {
            "header": CHAIN[13]["header"],
            "logs": [log],
            "transactions": [{"hash": tx(99), "from": BOB}],
        },
        {POOL: pool},
    )
    assert (
        found[0]["side"],
        found[0]["token_amount_raw"],
        found[0]["quote_amount_raw"],
    ) == (
        "buy",
        "70",
        "3",
    )


def test_swap_without_its_transaction_is_rejected():
    bare = {**CHAIN[13], "transactions": []}
    with pytest.raises(ChainScopeError, match="发起人"):
        block_swaps(bare, {POOL: POOL_INFO})


def test_attribution_direct_router_ambiguous_and_fee_mismatch():
    def check(number):
        transfers_ = block_transfers(CHAIN[number], TOKEN, ignore={POOL})
        legs = block_swaps(CHAIN[number], {POOL: POOL_INFO})
        return attribute(legs[0], transfers_)

    assert check(13)["attribution"] == "direct"
    assert check(18)["attribution"] == "router"
    carol = check(22)
    assert (carol["trader"], carol["attribution"]) == (CAROL, "ambiguous")
    assert all(check(n)["transfer_matched"] for n in (13, 17, 18, 22))
    taxed = block_swaps(CHAIN[17], {POOL: POOL_INFO})[0]
    sent = [{"from_address": ALICE, "to_address": POOL, "amount": 45}]
    assert not attribute(taxed, sent)["transfer_matched"]


def test_pool_logs_are_skipped_by_the_transfer_parser_only_when_tracked():
    assert len(block_transfers(CHAIN[13], TOKEN, ignore={POOL})) == 1
    with pytest.raises(ChainScopeError, match="过滤条件以外"):
        block_transfers(CHAIN[13], TOKEN)
    query = transfer_query(TOKEN, [{"address": [POOL], "transaction": True}])
    assert query["fields"]["transaction"] == {"hash": True, "from": True}
    assert "transaction" not in transfer_query(TOKEN)["fields"]


def run(engine, chain):
    return sync_job(
        engine, "bem", chain.portal(), sleep=lambda _: None, notify=lambda _: None
    )


def swap_rows(engine):
    with engine.connect() as connection:
        return connection.execute(
            sa.select(
                swaps.c.block_number, swaps.c.side, swaps.c.trader, swaps.c.attribution
            ).order_by(swaps.c.block_number)
        ).all()


@requires_mysql
def test_pools_discovered_later_backfill_swaps_without_duplicates(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = SwapChain()
    first = run(engine, chain)
    assert first["swaps"] == 0 and first["pools"] == 0
    assert count(engine, transfers) == 8

    found = pools_discover(engine, "bem", chain.portal())
    assert [(p["pool_address"], p["enabled"]) for p in found["added"]] == [
        (POOL, True),
        (JUNK, False),  # unknown quote token: waits for a person to enable it
    ]
    assert found["windows_reset"] == 3
    assert job_status(engine, "bem")["verified_through"] is None

    second = run(engine, chain)
    assert second["pools"] == 1 and second["swaps"] == 4 and second["inserted"] == 0
    assert swap_rows(engine) == [
        (13, "buy", BOB, "direct"),
        (17, "sell", ALICE, "direct"),
        (18, "buy", BOB, "router"),
        (22, "buy", CAROL, "ambiguous"),
    ]
    with engine.connect() as connection:
        counts = connection.execute(
            sa.select(windows.c.start_block, windows.c.swap_count).order_by(
                windows.c.start_block
            )
        ).all()
    assert counts == [(10, 1), (15, 2), (20, 1)]

    redo_job(engine, "bem", first=10, last=24)
    run(engine, chain)
    assert count(engine, swaps) == 4 and count(engine, transfers) == 8

    summary = swap_summary(engine, {"job_id": "bem"}, 10, 24)
    sides = {row["side"]: row for row in summary["by_side"]}
    assert sides["buy"]["swaps"] == 3 and sides["buy"]["token_amount_raw"] == "140"
    assert sides["sell"]["quote_amount_raw"] == "2"
    assert summary["traders"] == 3
    assert summary["attribution"] == {"direct": 2, "router": 1, "ambiguous": 1}

    result = set_pool_enabled(engine, "bem", POOL, False)
    assert result["swaps_removed"] == 4
    with engine.connect() as connection:
        assert list(load_pools(connection, "bem")) == []


@requires_mysql
def test_balances_top_holders_and_history(mysql_engine):
    engine = mysql_engine
    new_job(engine)
    chain = SwapChain()
    pools_discover(engine, "bem", chain.portal())
    run(engine, chain)

    report = top_holders(engine, "bem", top=80)
    assert report["as_of_block"] == 24
    assert [(h["address"], h["balance_raw"]) for h in report["top"]] == [
        (ALICE, "560"),
        (POOL, "310"),
        (BOB, "130"),
    ]
    assert report["holders"] == 3 and report["net_supply_raw"] == "1000"
    assert report["negative_balances"] == 0
    assert report["top"][0]["share_pct"] == 56.0
    assert report["top"][1]["category"] == "dex"
    assert report["top"][0]["balance"] == "0.00000000000000056"

    earlier = top_holders(engine, "bem", top=2, at_block=12)
    assert [(h["address"], h["balance_raw"]) for h in earlier["top"]] == [
        (ALICE, "600"),
        (POOL, "400"),
    ]
    with pytest.raises(ChainScopeError, match="已核验范围"):
        top_holders(engine, "bem", at_block=99)

    # Refreshing again adds nothing; a rewind forces a rebuild with equal results.
    assert refresh_balances(engine, "bem")["blocks_added"] == 0
    # A short rewind subtracts the rewound blocks in the same transaction.
    redo_job(engine, "bem", first=15, last=24)
    with engine.connect() as connection:
        assert connection.scalar(sa.select(jobs.c.balances_through)) == 14
    run(engine, chain)
    again = top_holders(engine, "bem")
    assert [h["balance_raw"] for h in again["top"]] == ["560", "310", "130"]


class StateRpc:
    """eth_call / eth_getCode answers for verify_holders and market checks."""

    def __init__(self, balances, supply, contracts=(), extra=None):
        self.balances = balances
        self.supply = supply
        self.contracts = set(contracts)
        self.extra = extra or {}
        self.blocks = []

    def handler(self, request):
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if method == "eth_getCode":
            result = "0x6080" if params[0] in self.contracts else "0x"
        elif method == "eth_blockNumber":
            result = hex(30)
        else:
            call = params[0]["data"]
            self.blocks.append(params[1])
            if call == "0x18160ddd":
                result = "0x" + word(self.supply)
            elif call.startswith("0x70a08231"):
                result = "0x" + word(self.balances.get("0x" + call[-40:], 0))
            else:
                result = self.extra[(params[0]["to"], call)]
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    def client(self):
        return RpcClient(
            "https://rpc.invalid",
            client=httpx.Client(transport=httpx.MockTransport(self.handler)),
            sleep=lambda _: None,
        )


@requires_mysql
def test_verify_holders_against_chain_state_and_record_contracts(
    mysql_engine, tmp_path
):
    engine = mysql_engine
    new_job(engine)
    chain = SwapChain()
    pools_discover(engine, "bem", chain.portal())
    run(engine, chain)
    state = StateRpc({ALICE: 560, POOL: 310, BOB: 130}, 1000, contracts={POOL})
    good = verify_holders(engine, state.client(), "bem", top=3)
    assert good["ok"] and good["supply_matches"] and good["holders_checked"] == 3
    assert set(state.blocks) == {hex(24)}
    report = top_holders(engine, "bem")
    assert [h["is_contract"] for h in report["top"]] == [False, True, False]

    # A reflection token changes balances without Transfers: verification fails.
    state = StateRpc({ALICE: 561, POOL: 310, BOB: 130}, 1001)
    bad = verify_holders(engine, state.client(), "bem", top=3)
    assert not bad["ok"] and bad["mismatched"][0]["address"] == ALICE
    assert "反射" in bad["hint"]

    labels = tmp_path / "labels.toml"
    labels.write_text(
        f'[[label]]\naddress = "{ALICE.upper().replace("0X", "0x")}"\n'
        'label = "某交易所热钱包"\ncategory = "cex"\nsource = "BscScan 公开标签"\n',
        encoding="utf-8",
    )
    assert import_labels(engine, labels) == {
        "imported": 1,
        "unverified": 1,
        "file": str(labels),
    }
    first = top_holders(engine, "bem")["top"][0]
    assert (first["label"], first["category"], first["label_verified"]) == (
        "某交易所热钱包",
        "cex",
        False,
    )
    labels.write_text(
        '[[label]]\naddress = "0x1"\nlabel = "x"\nsource = "y"\n', encoding="utf-8"
    )
    with pytest.raises(ChainScopeError, match="地址"):
        import_labels(engine, labels)
    labels.write_text(
        f'[[label]]\naddress = "{BOB}"\nlabel = "x"\ncategory = "whale"\nsource = "y"\n',
        encoding="utf-8",
    )
    with pytest.raises(ChainScopeError, match="category"):
        import_labels(engine, labels)
