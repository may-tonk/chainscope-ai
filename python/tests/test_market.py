"""CoinGecko / GeckoTerminal parsing, pacing, and the on-chain price check."""

from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa
from conftest import requires_mysql
from test_dex import POOL, POOL_INFO, WBNB, StateRpc, word
from test_jobs import TOKEN, new_job

from chainscope.dex import save_pools
from chainscope.errors import ChainScopeError
from chainscope.market import Http, coingecko, onchain_price, snapshot_market
from chainscope.models import market_snapshots, pool_ohlcv, pool_snapshots

E18 = 10**18


class Apis:
    def __init__(self, gt_price="0.002"):
        self.gt_price = gt_price
        self.seen = []
        self.limited = 1  # first request answers 429 once

    def handler(self, request):
        self.seen.append(request)
        if self.limited:
            self.limited -= 1
            return httpx.Response(429, headers={"Retry-After": "7"})
        path = request.url.path
        if "simple/token_price" in path:
            return httpx.Response(
                200,
                json={
                    TOKEN: {
                        "usd": 1.25,
                        "usd_market_cap": 0,
                        "usd_24h_vol": 3456.789,
                        "last_updated_at": 1_760_000_000,
                    }
                },
            )
        if path.endswith("/ohlcv/hour"):
            rows = [[1_760_014_400, "1.1", "1.3", "1.0", "1.2", "500.5"]]
            rows.append([1_760_000_000, "1.0", "1.2", "0.9", "1.1", "400"])
            return httpx.Response(
                200, json={"data": {"attributes": {"ohlcv_list": rows}}}
            )
        if path.endswith(f"/pools/{POOL}"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "attributes": {
                            "base_token_price_usd": "1.24",
                            "base_token_price_quote_token": self.gt_price,
                            "reserve_in_usd": "12345.678",
                            "volume_usd": {"h24": "999.99"},
                        },
                        "relationships": {
                            "base_token": {"data": {"id": f"bsc_{TOKEN}"}}
                        },
                    }
                },
            )
        return httpx.Response(404)

    def http(self, sleeps):
        return Http(
            httpx.Client(transport=httpx.MockTransport(self.handler)),
            sleep=sleeps.append,
            clock=lambda: 0.0,  # time stands still, so every gap is waited
        )


def test_coingecko_snapshot_waits_on_429_and_treats_zero_cap_as_unknown():
    sleeps = []
    apis = Apis()
    result = coingecko(apis.http(sleeps), TOKEN, "demo-key")
    assert sleeps[0] == 7
    assert apis.seen[-1].headers["x-cg-demo-api-key"] == "demo-key"
    assert result["listed"] and result["price_usd"] == Decimal("1.25")
    assert result["market_cap_usd"] is None
    assert result["volume_24h_usd"] == Decimal("3456.79")


def test_unlisted_token_is_recorded_as_not_listed():
    def handler(request):
        return httpx.Response(200, json={})

    http = Http(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None
    )
    assert coingecko(http, TOKEN) == {"source": "coingecko", "listed": False}


def test_bad_number_is_rejected():
    def handler(request):
        return httpx.Response(200, json={TOKEN: {"usd": "abc"}})

    http = Http(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None
    )
    with pytest.raises(ChainScopeError, match="数值"):
        coingecko(http, TOKEN)


def test_v3_price_from_slot0_in_both_orientations():
    sqrt_price = 2 * 2**96  # token1/token0 = 4
    pool = {**POOL_INFO, "dex": "pancake_v3"}
    state = StateRpc(
        {}, 0, extra={(POOL, "0x3850c7bd"): "0x" + word(sqrt_price) + word(0)}
    )
    rpc = state.client()
    assert onchain_price(rpc, pool, 18, 30)["onchain_price_quote"] == Decimal(4)
    flipped = {**pool, "token_is_token0": False}
    assert onchain_price(rpc, flipped, 18, 30)["onchain_price_quote"] == Decimal("0.25")


@requires_mysql
@pytest.mark.parametrize(("gt_price", "warned"), [("0.002", False), ("0.0021", True)])
def test_market_snapshot_saves_rows_and_flags_price_deviation(
    mysql_engine, gt_price, warned
):
    engine = mysql_engine
    new_job(engine)
    save_pools(
        engine,
        "bem",
        [
            {
                **POOL_INFO,
                "token0": TOKEN,
                "token1": WBNB,
                "quote_symbol": "WBNB",
                "fee": None,
                "created_block": 10,
            }
        ],
        "factory",
    )
    reserves = "0x" + word(1000 * E18) + word(2 * E18) + word(0)
    state = StateRpc({}, 0, extra={(POOL, "0x0902f1ac"): reserves})
    sleeps = []
    result = snapshot_market(
        engine, Apis(gt_price).http(sleeps), "bem", rpc=state.client(), ohlcv_limit=2
    )
    pool = result["pools"][0]
    assert pool["onchain_price_quote"] == Decimal("0.002")
    assert pool["reserve_block"] == 30 and pool["reserve_token_raw"] == str(1000 * E18)
    assert bool(result["warnings"]) is warned
    assert result["candles_saved"] == 2
    assert sleeps[0] == 7 and set(sleeps[1:]) == {2.5}  # paced for public limits
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(market_snapshots))
            == 1
        )
        assert connection.scalar(sa.select(pool_snapshots.c.reserve_usd)) == Decimal(
            "12345.68"
        )
        candles = connection.execute(
            sa.select(pool_ohlcv.c.bucket_start_utc, pool_ohlcv.c.close_usd).order_by(
                pool_ohlcv.c.bucket_start_utc
            )
        ).all()
    assert [c.close_usd for c in candles] == [Decimal("1.1"), Decimal("1.2")]
    # A second run updates the open candle instead of duplicating it.
    snapshot_market(
        engine, Apis(gt_price).http([]), "bem", rpc=state.client(), ohlcv_limit=2
    )
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(pool_ohlcv)) == 2
        )
