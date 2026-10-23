"""Balances rebuilt from collected Transfers, top holders, labels and checks.

address_balances is a cache of the job's verified Transfers (those linked to
its done windows) from the start block up to scan_jobs.balances_through, which
never passes verified_through. It advances by block range in chunks; every
chunk is one transaction that locks the job row, so concurrent refreshes and
retries cannot add the same range twice. A watermark rewind corrects it in the
same transaction (see jobs._rewind).
"""

import re
import time
import tomllib
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import insert as upsert
from sqlalchemy.exc import OperationalError

from . import balances as ledger
from .database import utcnow
from .errors import ChainScopeError
from .jobs import RETRYABLE, load_job
from .models import address_info, balances, jobs, labels, pools
from .parser import ADDRESS, fixed_hex, quantity

ZERO = ledger.ZERO
TOTAL_SUPPLY, BALANCE_OF = "0x18160ddd", "0x70a08231"
CATEGORIES = {"cex", "dex", "team", "burn", "contract", "bridge", "other"}
# About a day of BSC blocks per transaction keeps row locks short.
REFRESH_CHUNK_BLOCKS = 200_000

AT_BLOCK = f"""
SELECT holder, SUM(delta) AS balance FROM ({ledger.LEGS}) AS legs
WHERE holder <> :zero
GROUP BY holder
HAVING balance > 0
ORDER BY balance DESC
LIMIT :top
"""
AT_BLOCK_TOTALS = f"""
SELECT COUNT(*), COALESCE(SUM(balance), 0), SUM(balance < 0) FROM (
    SELECT holder, SUM(delta) AS balance FROM ({ledger.LEGS}) AS legs
    WHERE holder <> :zero GROUP BY holder HAVING balance <> 0
) AS b
"""


def _refresh_step(engine, job_id: str, chunk: int) -> dict:
    """Add one chunk of verified blocks to the balances, atomically."""
    with engine.begin() as connection:
        job = load_job(connection, job_id, lock=True)
        through, mark = job["verified_through"], job["balances_through"]
        if through is None or (mark is not None and mark >= through):
            return {"done": True, "mark": mark}
        rebuilt = mark is None
        if rebuilt:
            ledger.clear(connection, job["token_address"])
        first = job["start_block"] if rebuilt else mark + 1
        last = min(through, first + chunk - 1)
        ledger.apply_range(connection, job, first, last, 1)
        connection.execute(
            jobs.update().where(jobs.c.job_id == job_id).values(balances_through=last)
        )
    return {
        "done": last >= through,
        "mark": last,
        "rebuilt": rebuilt,
        "blocks": last - first + 1,
    }


def refresh_balances(
    engine,
    job_id: str,
    *,
    chunk: int = REFRESH_CHUNK_BLOCKS,
    stop=None,
    sleep=time.sleep,
) -> dict:
    """Bring address_balances up to verified_through, chunk by chunk.

    stop: optional callable; when it returns true the refresh pauses between
    chunks and the next call continues from the saved mark.
    """
    added, rebuilt, retries = 0, False, 0
    while True:
        try:
            step = _refresh_step(engine, job_id, chunk)
        except OperationalError as exc:
            code = exc.orig.args[0] if exc.orig is not None and exc.orig.args else None
            # The step is one transaction, so running it again cannot double count.
            if code not in RETRYABLE or retries >= 3:
                raise
            retries += 1
            sleep(2**retries)
            continue
        added += step.get("blocks", 0)
        rebuilt = rebuilt or step.get("rebuilt", False)
        if step["done"]:
            break
        if stop is not None and stop():
            return {
                "balances_through": step["mark"],
                "blocks_added": added,
                "rebuilt": rebuilt,
                "complete": False,
            }
    with engine.connect() as connection:
        job = load_job(connection, job_id)
    if job["verified_through"] is None:
        return {
            "balances_through": None,
            "note": "还没有已核验的窗口",
            "complete": True,
        }
    return {
        "balances_through": job["balances_through"],
        "blocks_added": added,
        "rebuilt": rebuilt,
        "complete": True,
    }


def tokens(raw: int | Decimal, decimals: int | None) -> str | None:
    if decimals is None:
        return None
    raw = int(raw)
    sign = "-" if raw < 0 else ""
    whole, fraction = divmod(abs(raw), 10**decimals)
    text = (
        f"{whole}.{fraction:0{decimals}d}".rstrip("0").rstrip(".")
        if decimals
        else str(whole)
    )
    return sign + text


def describe(connection, job_id: str, addresses: list[str]) -> dict:
    """Label, category and contract flag of each address, if known."""
    if not addresses:
        return {}
    found = {
        a: {
            "label": None,
            "category": None,
            "label_verified": None,
            "is_contract": None,
        }
        for a in addresses
    }
    for row in connection.execute(
        sa.select(labels).where(
            labels.c.chain_id == 56, labels.c.address.in_(addresses)
        )
    ).mappings():
        found[row["address"]].update(
            label=row["label"], category=row["category"], label_verified=row["verified"]
        )
    for row in connection.execute(
        sa.select(pools).where(
            pools.c.job_id == job_id, pools.c.pool_address.in_(addresses)
        )
    ).mappings():
        quote = row["quote_symbol"] or row["quote_token"][:10]
        found[row["pool_address"]].update(
            label=f"{row['dex']} 池子（报价 {quote}）",
            category="dex",
            # The factory event proves it; a manual pool was checked on-chain.
            label_verified=True,
        )
    for row in connection.execute(
        sa.select(address_info.c.address, address_info.c.is_contract).where(
            address_info.c.chain_id == 56, address_info.c.address.in_(addresses)
        )
    ):
        found[row.address]["is_contract"] = bool(row.is_contract)
    return found


def top_holders(
    engine, job_id: str, *, top: int = 80, at_block: int | None = None
) -> dict:
    if not 1 <= top <= 1000:
        raise ChainScopeError("top 需在 1 到 1000 之间")
    if at_block is None:
        refresh_balances(engine, job_id)
    # One read transaction: the mark and the rows come from the same snapshot.
    with engine.connect() as connection:
        job = load_job(connection, job_id)
        token, decimals = job["token_address"], job["token_decimals"]
        if at_block is None:
            as_of = job["balances_through"]
            if as_of is None:
                raise ChainScopeError("任务还没有已核验的窗口，无法统计持仓")
            where = [balances.c.chain_id == 56, balances.c.token_address == token]
            rows = connection.execute(
                sa.select(balances.c.address, balances.c.balance_raw)
                .where(*where, balances.c.balance_raw > 0)
                .order_by(balances.c.balance_raw.desc())
                .limit(top)
            ).all()
            holders, supply, negative = connection.execute(
                sa.select(
                    sa.func.count(),
                    sa.func.coalesce(sa.func.sum(balances.c.balance_raw), 0),
                    sa.func.coalesce(
                        sa.func.sum(sa.cast(balances.c.balance_raw < 0, sa.Integer)), 0
                    ),
                ).where(*where, balances.c.balance_raw != 0)
            ).one()
        else:
            through = job["verified_through"]
            if through is None or not job["start_block"] <= at_block <= through:
                raise ChainScopeError(
                    f"区块 {at_block} 不在已核验范围内（{job['start_block']}–{through}）"
                )
            as_of = at_block
            ledger.capacity(connection, job)
            params = {
                **ledger.range_params(job, job["start_block"], at_block),
                "window_first": job["start_block"],
                "top": top,
            }
            rows = connection.execute(sa.text(AT_BLOCK), params).all()
            holders, supply, negative = connection.execute(
                sa.text(AT_BLOCK_TOTALS), params
            ).one()
        info = describe(connection, job_id, [r[0] for r in rows])
    supply = int(supply)
    ranked = []
    for rank, (address, raw) in enumerate(rows, 1):
        raw = int(raw)
        ranked.append(
            {
                "rank": rank,
                "address": address,
                "balance_raw": str(raw),
                "balance": tokens(raw, decimals),
                "share_pct": round(raw * 100 / supply, 4) if supply > 0 else None,
                **info[address],
            }
        )
    return {
        "job_id": job_id,
        "as_of_block": as_of,
        # Holders with a nonzero balance; the zero address (mints) is excluded.
        "holders": int(holders) - int(negative or 0),
        "net_supply_raw": str(supply),
        "net_supply": tokens(supply, decimals),
        "negative_balances": int(negative or 0),
        # Balances are only whole if collection started at the deploy block.
        "history_complete": job["deploy_block"] is not None
        and job["start_block"] <= job["deploy_block"],
        "note": "持仓由已核验窗口的 Transfer 累加得到；负余额表示数据缺口或非标准代币；"
        "history_complete 为 false 时起点晚于部署，余额不完整",
        "top": ranked,
    }


def eth_call_uint(rpc, to: str, data: str, block: int) -> int:
    return quantity(
        rpc.call("eth_call", [{"to": to, "data": data}, hex(block)]), "eth_call"
    )


def record_code(connection, rpc, addresses: list[str], block: int) -> dict:
    """eth_getCode at `block` for each address, saved to address_info."""
    flags = {}
    now = utcnow()
    for address in addresses:
        code = rpc.call("eth_getCode", [address, hex(block)])
        if not isinstance(code, str) or not re.fullmatch(r"0x[0-9a-fA-F]*", code):
            raise ChainScopeError("eth_getCode 返回格式错误")
        size = (len(code) - 2) // 2
        # 0xef0100 is an EIP-7702 delegation: still an externally owned account.
        contract = size > 0 and not code.lower().startswith("0xef0100")
        flags[address] = contract
        row = {
            "chain_id": 56,
            "address": address,
            "is_contract": contract,
            "code_size": size,
            "checked_block": block,
            "checked_at_utc": now,
        }
        connection.execute(
            upsert(address_info)
            .values(**row)
            .on_duplicate_key_update(
                is_contract=row["is_contract"],
                code_size=row["code_size"],
                checked_block=row["checked_block"],
                checked_at_utc=row["checked_at_utc"],
            )
        )
    return flags


def verify_holders(engine, rpc, job_id: str, *, top: int = 20) -> dict:
    """Compare totalSupply and the top balances with on-chain state at the mark."""
    report = top_holders(engine, job_id, top=top)
    block = report["as_of_block"]
    with engine.connect() as connection:
        job = load_job(connection, job_id)
    token = job["token_address"]
    on_chain_supply = eth_call_uint(rpc, token, TOTAL_SUPPLY, block)
    mismatched = []
    for holder in report["top"]:
        data = BALANCE_OF + "0" * 24 + holder["address"][2:]
        actual = eth_call_uint(rpc, token, data, block)
        if actual != int(holder["balance_raw"]):
            mismatched.append(
                {
                    "address": holder["address"],
                    "collected_raw": holder["balance_raw"],
                    "on_chain_raw": str(actual),
                }
            )
    with engine.begin() as connection:
        record_code(connection, rpc, [h["address"] for h in report["top"]], block)
    supply_matches = on_chain_supply == int(report["net_supply_raw"])
    return {
        "job_id": job_id,
        "block": block,
        "total_supply_on_chain_raw": str(on_chain_supply),
        "net_supply_collected_raw": report["net_supply_raw"],
        "supply_matches": supply_matches,
        "holders_checked": len(report["top"]),
        "mismatched": mismatched,
        "ok": supply_matches and not mismatched,
        "hint": None
        if supply_matches and not mismatched
        else "不一致常见原因：反射/分红型代币（余额不经 Transfer 变化）、"
        "rebase、任务起点晚于部署区块，或采集有缺口",
    }


def import_labels(engine, path: Path) -> dict:
    """Load [[label]] entries from a TOML file; existing addresses are updated."""
    try:
        entries = tomllib.loads(path.read_text(encoding="utf-8")).get("label", [])
    except tomllib.TOMLDecodeError as exc:
        raise ChainScopeError(f"标签文件格式错误：{exc}") from None
    if not isinstance(entries, list):
        raise ChainScopeError("标签文件需要使用 [[label]] 列表")
    rows = []
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ChainScopeError(f"第 {number} 个标签格式错误")
        category = entry.get("category", "other")
        if category not in CATEGORIES:
            raise ChainScopeError(
                f"第 {number} 个标签的 category 不在 {sorted(CATEGORIES)} 中"
            )
        label, source = entry.get("label"), entry.get("source")
        if (
            not isinstance(label, str)
            or not label
            or not isinstance(source, str)
            or not source
        ):
            raise ChainScopeError(
                f"第 {number} 个标签缺少 label 或 source（来源必须写明）"
            )
        rows.append(
            {
                "chain_id": 56,
                "address": fixed_hex(
                    entry.get("address"), ADDRESS, f"第 {number} 个标签地址"
                ),
                "label": label[:64],
                "category": category,
                "source": source[:200],
                "verified": entry.get("verified") is True,
                "updated_at_utc": utcnow(),
            }
        )
    with engine.begin() as connection:
        for row in rows:
            connection.execute(
                upsert(labels)
                .values(**row)
                .on_duplicate_key_update(
                    **{
                        k: row[k]
                        for k in (
                            "label",
                            "category",
                            "source",
                            "verified",
                            "updated_at_utc",
                        )
                    }
                )
            )
    return {
        "imported": len(rows),
        "unverified": sum(not r["verified"] for r in rows),
        "file": str(path),
    }
