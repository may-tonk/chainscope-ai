"""Market context: CoinGecko price, GeckoTerminal pools and 4h candles, on-chain check.

These are third-party aggregates, kept apart from the on-chain event tables.
The on-chain pool price (V2 reserves or V3 slot0) is read at a known block and
compared with GeckoTerminal's, so a stale or wrong aggregate shows up as a
deviation instead of passing silently.
"""

import time
from datetime import UTC, datetime
from decimal import Context, Decimal, InvalidOperation, localcontext

import httpx
from sqlalchemy.dialects.mysql import insert as upsert

from .database import utcnow
from .dex import KNOWN_QUOTES, load_pools, reserves, word
from .errors import ChainScopeError
from .jobs import load_job
from .models import market_snapshots, pool_ohlcv, pool_snapshots
from .parser import quantity

COINGECKO = "https://api.coingecko.com/api/v3"
GECKOTERMINAL = "https://api.geckoterminal.com/api/v2"
SLOT0 = "0x3850c7bd"
DECIMALS = "0x313ce567"
PRICE = Decimal("1e-30")
USD = Decimal("0.01")
MAX_DEVIATION_PCT = 2
# 30 decimal places plus the integer part exceed the default 28 digits.
WIDE = Context(prec=80)


class Http:
    """Paced GET with Retry-After; public APIs allow about 30 requests a minute."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        sleep=time.sleep,
        clock=time.monotonic,
        min_interval=2.5,
    ):
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self._min_interval = min_interval
        self._last = None
        self.requests = 0

    def get(self, url: str, *, params=None, headers=None):
        for attempt in range(4):
            if self._last is not None:
                gap = self._min_interval - (self._clock() - self._last)
                if gap > 0:
                    self._sleep(gap)
            self._last = self._clock()
            self.requests += 1
            try:
                response = self._client.get(url, params=params, headers=headers)
            except httpx.TransportError:
                if attempt == 3:
                    raise ChainScopeError(
                        f"{url.split('/')[2]}: 网络连接失败"
                    ) from None
                self._sleep(2**attempt)
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                after = response.headers.get("Retry-After", "").strip()
                self._sleep(
                    min(int(after), 120) if after.isdigit() else 2 ** (attempt + 2)
                )
                continue
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise ChainScopeError(
                    f"{url.split('/')[2]}: HTTP {response.status_code}"
                )
            try:
                return response.json()
            except ValueError:
                raise ChainScopeError(
                    f"{url.split('/')[2]}: 返回内容不是 JSON"
                ) from None
        raise AssertionError("unreachable retry state")


def number(value, step: Decimal) -> Decimal | None:
    if value is None:
        return None
    try:
        with localcontext(WIDE):
            return Decimal(str(value)).quantize(step)
    except (InvalidOperation, ValueError):
        raise ChainScopeError(f"行情数值格式错误：{value!r}") from None


def coingecko(http: Http, token: str, api_key: str = "") -> dict:
    body = http.get(
        f"{COINGECKO}/simple/token_price/binance-smart-chain",
        params={
            "contract_addresses": token,
            "vs_currencies": "usd",
            "include_market_cap": "true",
            "include_24hr_vol": "true",
            "include_last_updated_at": "true",
        },
        headers={"x-cg-demo-api-key": api_key} if api_key else None,
    )
    data = (body or {}).get(token) if isinstance(body, dict) else None
    if not data or data.get("usd") is None:
        return {"source": "coingecko", "listed": False}
    updated = data.get("last_updated_at")
    return {
        "source": "coingecko",
        "listed": True,
        "price_usd": number(data["usd"], PRICE),
        # CoinGecko reports 0 when it does not know the circulating supply.
        "market_cap_usd": number(data.get("usd_market_cap") or None, USD),
        "volume_24h_usd": number(data.get("usd_24h_vol"), USD),
        "source_updated_at_utc": datetime.fromtimestamp(updated, UTC).replace(
            tzinfo=None
        )
        if isinstance(updated, int)
        else None,
    }


def pool_market(http: Http, pool: str, token: str) -> dict | None:
    """GeckoTerminal price of `token` in USD and in the pool's other token."""
    body = http.get(f"{GECKOTERMINAL}/networks/bsc/pools/{pool}")
    if not body:
        return None
    try:
        data = body["data"]
        attributes = data["attributes"]
        base = (
            data["relationships"]["base_token"]["data"]["id"].split("_", 1)[1].lower()
        )
    except (KeyError, TypeError, IndexError, AttributeError):
        raise ChainScopeError(f"GeckoTerminal 池子 {pool} 返回格式错误") from None
    is_base = base == token
    return {
        "price_usd": number(
            attributes.get(
                "base_token_price_usd" if is_base else "quote_token_price_usd"
            ),
            PRICE,
        ),
        "price_quote": number(
            attributes.get(
                "base_token_price_quote_token"
                if is_base
                else "quote_token_price_base_token"
            ),
            PRICE,
        ),
        "reserve_usd": number(attributes.get("reserve_in_usd"), USD),
        "volume_24h_usd": number((attributes.get("volume_usd") or {}).get("h24"), USD),
    }


def candles(http: Http, pool: str, token: str, limit: int) -> list[dict]:
    """4-hour candles of `token` priced in USD, oldest first."""
    body = http.get(
        f"{GECKOTERMINAL}/networks/bsc/pools/{pool}/ohlcv/hour",
        params={"aggregate": 4, "limit": limit, "currency": "usd", "token": token},
    )
    try:
        rows = body["data"]["attributes"]["ohlcv_list"] if body else []
    except (KeyError, TypeError):
        raise ChainScopeError(f"GeckoTerminal K 线 {pool} 返回格式错误") from None
    result = []
    for stamp, open_, high, low, close, volume in rows:
        result.append(
            {
                "pool_address": pool,
                "timeframe": "4h",
                "bucket_start_utc": datetime.fromtimestamp(int(stamp), UTC).replace(
                    tzinfo=None
                ),
                "open_usd": number(open_, PRICE),
                "high_usd": number(high, PRICE),
                "low_usd": number(low, PRICE),
                "close_usd": number(close, PRICE),
                "volume_usd": number(volume, USD),
            }
        )
    return sorted(result, key=lambda r: r["bucket_start_utc"])


def quote_decimals(rpc, quote: str) -> int:
    if quote in KNOWN_QUOTES:
        return 18
    return quantity(
        rpc.call("eth_call", [{"to": quote, "data": DECIMALS}, "latest"]),
        "decimals",
        255,
    )


def onchain_price(rpc, pool: dict, token_decimals: int, block: int) -> dict:
    """Price of the token in the quote token, from reserves (V2) or slot0 (V3)."""
    with localcontext(WIDE):
        return _onchain_price(rpc, pool, token_decimals, block)


def _onchain_price(rpc, pool: dict, token_decimals: int, block: int) -> dict:
    quote_dec = quote_decimals(rpc, pool["quote_token"])
    if "v2" in pool["dex"]:
        token_raw, quote_raw = reserves(rpc, pool, block)
        price = (
            Decimal(quote_raw)
            / 10**quote_dec
            / (Decimal(token_raw) / 10**token_decimals)
            if token_raw
            else None
        )
        return {
            "reserve_token_raw": str(token_raw),
            "reserve_quote_raw": str(quote_raw),
            "onchain_price_quote": None if price is None else price.quantize(PRICE),
        }
    raw = rpc.call(
        "eth_call", [{"to": pool["pool_address"], "data": SLOT0}, hex(block)]
    )
    if not isinstance(raw, str) or len(raw) < 66:
        raise ChainScopeError("slot0 返回格式错误")
    sqrt_price = word(raw, 0)
    # token1 per token0 in raw units is (sqrtPriceX96 / 2^96)^2.
    ratio = Decimal(sqrt_price) ** 2 / Decimal(2) ** 192
    dec0, dec1 = (
        (token_decimals, quote_dec)
        if pool["token_is_token0"]
        else (quote_dec, token_decimals)
    )
    one_per_zero = ratio * Decimal(10) ** (dec0 - dec1)
    price = (
        one_per_zero
        if pool["token_is_token0"]
        else (1 / one_per_zero if one_per_zero else None)
    )
    return {"onchain_price_quote": None if price is None else price.quantize(PRICE)}


def deviation(onchain, aggregate) -> Decimal | None:
    if onchain is None or not aggregate:
        return None
    with localcontext(WIDE):
        return ((onchain - aggregate) / aggregate * 100).quantize(Decimal("0.0001"))


def snapshot_market(
    engine,
    http: Http,
    job_id: str,
    *,
    rpc=None,
    api_key: str = "",
    ohlcv_limit: int = 180,
) -> dict:
    """One CoinGecko snapshot, plus a snapshot and 4h candles per enabled pool."""
    with engine.connect() as connection:
        job = load_job(connection, job_id)
        pools = load_pools(connection, job_id)
    token = job["token_address"]
    now = utcnow()
    market = {
        "token_address": token,
        "fetched_at_utc": now,
        **coingecko(http, token, api_key),
    }
    block = quantity(rpc.call("eth_blockNumber", []), "block") if rpc else None
    pool_rows, candle_rows, warnings = [], [], []
    for address, pool in pools.items():
        row = {"pool_address": address, "fetched_at_utc": now}
        gecko = pool_market(http, address, token)
        if gecko is None:
            warnings.append(f"GeckoTerminal 没有收录池子 {address}")
        else:
            row.update(gecko)
        if rpc is not None and job["token_decimals"] is not None:
            row["reserve_block"] = block
            row.update(onchain_price(rpc, pool, job["token_decimals"], block))
            row["deviation_pct"] = deviation(
                row.get("onchain_price_quote"), row.get("price_quote")
            )
            if (
                row["deviation_pct"] is not None
                and abs(row["deviation_pct"]) > MAX_DEVIATION_PCT
            ):
                warnings.append(
                    f"池子 {address} 链上价格与 GeckoTerminal 相差 {row['deviation_pct']}%"
                )
        pool_rows.append(row)
        if ohlcv_limit:
            candle_rows += [
                {**c, "fetched_at_utc": now}
                for c in candles(http, address, token, ohlcv_limit)
            ]
    with engine.begin() as connection:
        connection.execute(market_snapshots.insert().values(**market))
        for row in pool_rows:
            connection.execute(pool_snapshots.insert().values(**row))
        for candle in candle_rows:
            # The latest bucket is still open, so later fetches overwrite it.
            connection.execute(
                upsert(pool_ohlcv)
                .values(**candle)
                .on_duplicate_key_update(
                    **{
                        k: candle[k]
                        for k in (
                            "open_usd",
                            "high_usd",
                            "low_usd",
                            "close_usd",
                            "volume_usd",
                            "fetched_at_utc",
                        )
                    }
                )
            )
    return {
        "job_id": job_id,
        "market": market,
        "pools": pool_rows,
        "candles_saved": len(candle_rows),
        "warnings": warnings,
        "requests": http.requests,
        "note": "价格、市值和成交量来自第三方聚合，仅作参考；以链上数据为准",
    }
