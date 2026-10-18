"""Bounded, durable block metadata without downloading Transfer log bodies."""

from datetime import UTC, datetime

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import insert as upsert

from .errors import ChainScopeError, EventConflict, ReconcileMismatch
from .models import block_headers, transfers
from .parser import HASH, fixed_hex, quantity
from .sqd import BLOCK_FIELDS
from .validator import TRANSFER_TOPIC


def header_row(number, block_hash, stamp):
    return dict(
        chain_id=56,
        block_number=number,
        block_hash=fixed_hex(block_hash, HASH, "block.hash"),
        block_time_utc=datetime.fromtimestamp(stamp, UTC).replace(tzinfo=None),
    )


def save_headers(engine, rows):
    if not rows:
        return
    with engine.begin() as c:
        existing = {
            r["block_number"]: r
            for r in c.execute(
                sa.select(block_headers)
                .where(
                    block_headers.c.chain_id == 56,
                    block_headers.c.block_number.in_([r["block_number"] for r in rows]),
                )
                .with_for_update()
            ).mappings()
        }
        for row in rows:
            old = existing.get(row["block_number"])
            if old and any(old[k] != row[k] for k in ("block_hash", "block_time_utc")):
                raise EventConflict(
                    "已确认区块的哈希或时间发生冲突",
                    [
                        {
                            "kind": "header_conflict",
                            "detail": {"block_number": row["block_number"]},
                        }
                    ],
                )
        c.execute(
            upsert(block_headers)
            .values(rows)
            .on_duplicate_key_update(block_number=block_headers.c.block_number)
        )


def confirm_transfer_times(staged, headers, rpc, stop):
    """Confirm discrepant metadata against RPC, without rewriting the source rows."""
    confirmed = {}
    evidence = []
    for item in staged:
        number = item["block_number"]
        header = headers[number]
        original = item["block_time_utc"]
        if original is None or original == header["block_time_utc"]:
            continue
        if stop():
            return None
        if number not in confirmed:
            raw = rpc.call("eth_getBlockByNumber", [hex(number), False])
            if (
                not isinstance(raw, dict)
                or quantity(raw.get("number"), "block.number") != number
            ):
                raise ChainScopeError("时间复核所需的主节点区块头缺失或高度不符")
            canonical = header_row(
                number,
                raw.get("hash"),
                quantity(raw.get("timestamp"), "timestamp"),
            )
            if any(canonical[k] != header[k] for k in ("block_hash", "block_time_utc")):
                raise ReconcileMismatch(
                    "时间复核时主节点与缓存区块头不一致，保留隔离",
                    [{"kind": "header_conflict", "detail": {"block_number": number}}],
                )
            confirmed[number] = canonical
        evidence.append(
            {
                "tx_hash": item["tx_hash"],
                "log_index": item["log_index"],
                "detail": {
                    "block_number": number,
                    "block_hash": header["block_hash"],
                    "source_time_utc": original.isoformat(),
                    "confirmed_time_utc": header["block_time_utc"].isoformat(),
                    "confirmation_source": "alchemy_eth_getBlockByNumber",
                },
            }
        )
    return evidence


def complete_headers(engine, job, window, staged, portal, rpc, stop):
    """SQD returns only headers; RPC fills missing headers if SQD is unavailable.

    No Transfer body is requested, counted or compared. Each block is cached
    durably, so stopping during metadata work never requires redoing the pages.
    """
    needed = {r["block_number"] for r in staged}
    if not needed:
        return {}
    first, last = window["start_block"], window["end_block"]
    with engine.connect() as c:
        found = {
            r["block_number"]: dict(r)
            for r in c.execute(
                sa.select(block_headers).where(
                    block_headers.c.chain_id == 56,
                    block_headers.c.block_number.between(first, last),
                )
            ).mappings()
        }
        # Reuse old formal metadata too; never alter those events.
        old = c.execute(
            sa.select(
                transfers.c.block_number,
                transfers.c.block_hash,
                transfers.c.block_time_utc,
            )
            .where(
                transfers.c.chain_id == 56,
                transfers.c.token_address == job["token_address"],
                transfers.c.block_number.between(first, last),
            )
            .distinct()
        ).mappings()
        for row in old:
            if row["block_number"] not in found:
                found[row["block_number"]] = dict(row, chain_id=56)
    if needed - found.keys() and not stop():
        query = {
            "type": "evm",
            "fields": {"block": BLOCK_FIELDS},
            "logs": [{"address": [job["token_address"]], "topic0": [TRANSFER_TOPIC]}],
        }
        blocks = portal.stream(query, first, last)
        buffered = []
        try:
            for block in blocks:
                h = block["header"]
                n, stamp = h["number"], h.get("timestamp")
                if not isinstance(stamp, int) or isinstance(stamp, bool) or stamp < 0:
                    raise ChainScopeError("区块头缺少有效 timestamp")
                if n in needed and n not in found:
                    row = header_row(n, h.get("hash"), stamp)
                    found[n] = row
                    buffered.append(row)
                if len(buffered) >= 500:
                    save_headers(engine, buffered)
                    buffered.clear()
                if stop():
                    break
        except (httpx.HTTPError, ChainScopeError) as exc:
            if isinstance(exc, EventConflict):
                raise
            # Metadata availability is not proof that a Transfer is absent.
            # Fall back to the primary node, with the same durable cache.
        finally:
            blocks.close()
            save_headers(engine, buffered)
    for number in sorted(needed - found.keys()):
        if stop():
            return None
        h = rpc.call("eth_getBlockByNumber", [hex(number), False])
        if (
            not isinstance(h, dict)
            or quantity(h.get("number"), "block.number") != number
        ):
            raise ChainScopeError("主节点区块头缺失或高度不符")
        row = header_row(
            number, h.get("hash"), quantity(h.get("timestamp"), "timestamp")
        )
        save_headers(engine, [row])
        found[number] = row
    return found if not stop() else None
