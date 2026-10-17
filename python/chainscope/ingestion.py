"""Commit bounded Alchemy pages without admitting them to research statistics.

An opaque page cursor and its rows commit together. Expired cursors restart
only their window. Older generations remain inspectable but never participate
in a new generation's completeness check or in formal window_events links.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import insert as upsert

from .collector import transfer_params
from .database import utcnow
from .errors import BlockTooLarge, ChainScopeError, EventConflict
from .jobs import QUARANTINE, fault, load_job
from .models import ingestion, windows
from .models import pending_transfers as pending
from .parser import quantity, transfer_identity
from .sqd import TooManyEvents

CURSOR_LIFETIME = timedelta(minutes=8)  # Provider TTL is ten minutes.
PAGE_LIMIT = 100
FIELDS = ("block_number", "from_address", "to_address", "amount_raw", "block_time_utc")


def where(table, job_id, start):
    return (table.c.job_id == job_id) & (table.c.start_block == start)


def checkpoint(connection, job_id, start):
    row = (
        connection.execute(sa.select(ingestion).where(where(ingestion, job_id, start)))
        .mappings()
        .first()
    )
    return dict(row) if row else None


def prepare(engine, job_id, window, *, now=None, provider="alchemy"):
    now = now or utcnow()
    start, end = window["start_block"], window["end_block"]
    with engine.begin() as c:
        saved = checkpoint(c, job_id, start)
        expired = (
            saved
            and saved["page_key"]
            and now - saved["updated_at_utc"] >= CURSOR_LIFETIME
        )
        if (
            saved
            and saved["end_block"] == end
            and (saved["complete"] or (not expired and saved["provider"] == provider))
        ):
            return saved
        row = dict(
            job_id=job_id,
            start_block=start,
            end_block=end,
            generation=str(uuid4()),
            provider=provider,
            page_key=None,
            seen_keys=[],
            pages=0,
            event_count=0,
            complete=False,
            updated_at_utc=now,
        )
        c.execute(
            upsert(ingestion)
            .values(**row)
            .on_duplicate_key_update(
                **{k: v for k, v in row.items() if k not in ("job_id", "start_block")}
            )
        )
        return row


def parse_pending(raw, job, window):
    item = transfer_identity(
        raw, job["token_address"], window["start_block"], window["end_block"]
    )
    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ChainScopeError("Transfers metadata 格式错误")
    stamp = metadata.get("blockTimestamp")
    moment = None
    if stamp is not None:
        try:
            moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                raise ValueError("timezone missing")
            moment = moment.astimezone(UTC).replace(tzinfo=None)
        except (AttributeError, TypeError, ValueError):
            raise ChainScopeError("Transfers 时间戳格式错误") from None
    decimals = raw["rawContract"].get("decimal")
    if decimals is not None and job["token_decimals"] is not None:
        if quantity(decimals, "decimals", 255) != job["token_decimals"]:
            raise ChainScopeError("Alchemy 返回的 decimals 与 BEM 任务配置不一致")
    return {
        k: item[k]
        for k in ("tx_hash", "log_index", "block_number", "from_address", "to_address")
    } | {
        "amount_raw": str(item["amount"]),
        "block_time_utc": moment,
    }


def conflict(old, new):
    return {
        f: {"stored": str(old[f]), "fetched": str(new[f])}
        for f in FIELDS
        if old[f] != new[f]
    }


def save_page(engine, job_id, window, previous, rows, next_key):
    start = window["start_block"]
    by_id = {}
    for row in rows:
        key = (row["tx_hash"], row["log_index"])
        if key in by_id and conflict(by_id[key], row):
            raise EventConflict(
                "Alchemy 同页事件内容冲突",
                [
                    {
                        "kind": "field_conflict",
                        "tx_hash": key[0],
                        "log_index": key[1],
                        "detail": {},
                    }
                ],
            )
        by_id[key] = row
    with engine.begin() as c:
        locked = (
            c.execute(
                sa.select(ingestion)
                .where(where(ingestion, job_id, start))
                .with_for_update()
            )
            .mappings()
            .one()
        )
        if (
            locked["generation"] != previous["generation"]
            or locked["pages"] != previous["pages"]
        ):
            raise ChainScopeError("分页进度已变化，请重新读取后继续")
        existing = {}
        if by_id:
            existing = {
                (r["tx_hash"], r["log_index"]): r
                for r in c.execute(
                    sa.select(pending).where(
                        where(pending, job_id, start),
                        sa.tuple_(pending.c.tx_hash, pending.c.log_index).in_(
                            list(by_id)
                        ),
                    )
                ).mappings()
            }
        for key, row in by_id.items():
            if key in existing and (changed := conflict(existing[key], row)):
                raise EventConflict(
                    "Alchemy 重取事件与已保存待核验数据冲突，不覆盖",
                    [
                        {
                            "kind": "field_conflict",
                            "tx_hash": key[0],
                            "log_index": key[1],
                            "detail": {"fields": changed},
                        }
                    ],
                )
        values = [
            dict(
                job_id=job_id, start_block=start, generation=previous["generation"], **r
            )
            for r in by_id.values()
        ]
        if values:
            c.execute(
                upsert(pending)
                .values(values)
                .on_duplicate_key_update(generation=previous["generation"])
            )
        count = c.scalar(
            sa.select(sa.func.count())
            .select_from(pending)
            .where(
                where(pending, job_id, start),
                pending.c.generation == previous["generation"],
            )
        )
        hashes = list(previous["seen_keys"])
        if next_key is not None:
            hashes.append(hashlib.sha256(next_key.encode()).hexdigest())
        row = dict(
            previous,
            page_key=next_key,
            seen_keys=hashes,
            pages=previous["pages"] + 1,
            event_count=count,
            complete=next_key is None,
            updated_at_utc=utcnow(),
        )
        c.execute(
            ingestion.update()
            .where(where(ingestion, job_id, start))
            .values(
                **{k: v for k, v in row.items() if k not in ("job_id", "start_block")}
            )
        )
        fault("before_page_commit")
    fault("after_page_commit")
    return row


def collect_window(
    engine,
    job,
    window,
    rpc,
    *,
    limit=50_000,
    hard_limit=50_000,
    stop=lambda: False,
    notify=lambda _: None,
):
    saved = prepare(engine, job["job_id"], window)
    if saved["complete"]:
        if saved["event_count"] > limit and window["end_block"] > window["start_block"]:
            raise TooManyEvents(saved["event_count"], window["start_block"])
        if saved["event_count"] > hard_limit:
            raise BlockTooLarge("已保存窗口超过当前单区块硬上限；待人工核实")
    while not saved["complete"]:
        if stop():
            return False
        if saved["pages"] >= PAGE_LIMIT:
            if window["start_block"] < window["end_block"]:
                raise TooManyEvents(saved["event_count"], window["start_block"])
            raise BlockTooLarge("单区块分页超过安全上限，待人工核实")
        params = transfer_params(
            job["token_address"], window["start_block"], window["end_block"]
        )
        if saved["page_key"]:
            params["pageKey"] = saved["page_key"]
        result = rpc.call("alchemy_getAssetTransfers", [params])
        if not isinstance(result, dict) or not isinstance(
            result.get("transfers"), list
        ):
            raise ChainScopeError("Transfers 响应缺少 transfers 列表")
        if len(result["transfers"]) > 1000:
            raise ChainScopeError("Alchemy 页超过请求的 1000 条上限")
        key = result.get("pageKey") or None
        if key is not None and (
            not isinstance(key, str)
            or len(key) > 4096
            or hashlib.sha256(key.encode()).hexdigest() in saved["seen_keys"]
        ):
            raise ChainScopeError("Transfers 分页游标无效或重复")
        rows = [parse_pending(raw, job, window) for raw in result["transfers"]]
        # One extra bounded page is committed before testing the unique count;
        # it remains durable evidence if this window subsequently needs a split.
        saved = save_page(engine, job["job_id"], window, saved, rows, key)
        if saved["complete"] or saved["pages"] % 10 == 0:
            notify(
                {
                    "alchemy_page": [window["start_block"], window["end_block"]],
                    "pages": saved["pages"],
                    "pending_events": saved["event_count"],
                    "complete": saved["complete"],
                }
            )
        held = saved["event_count"]
        if held > limit and window["end_block"] > window["start_block"]:
            raise TooManyEvents(held, window["start_block"])
        if held > hard_limit:
            raise BlockTooLarge("单区块转账数超过硬上限；待人工核实")
    return True


def pending_rows(engine, job_id, window):
    with engine.connect() as c:
        saved = checkpoint(c, job_id, window["start_block"])
        if (
            not saved
            or not saved["complete"]
            or saved["end_block"] != window["end_block"]
        ):
            raise ChainScopeError("窗口的 Alchemy 转账尚未完整保存")
        rows = list(
            c.execute(
                sa.select(pending).where(
                    where(pending, job_id, window["start_block"]),
                    pending.c.generation == saved["generation"],
                )
            ).mappings()
        )
        if len(rows) != saved["event_count"]:
            raise ChainScopeError("待核验页进度与数据数量不一致，不提交正式统计")
        return [{**r, "amount": int(r["amount_raw"])} for r in rows]


def collection_progress(connection, job_id):
    job = load_job(connection, job_id)
    rows = (
        connection.execute(
            sa.select(
                windows.c.start_block,
                windows.c.end_block,
                windows.c.status,
                ingestion.c.end_block.label("collected_end"),
                ingestion.c.complete,
                ingestion.c.event_count,
            )
            .select_from(
                windows.outerjoin(
                    ingestion,
                    (windows.c.job_id == ingestion.c.job_id)
                    & (windows.c.start_block == ingestion.c.start_block),
                )
            )
            .where(windows.c.job_id == job_id)
            .order_by(windows.c.start_block)
        )
        .mappings()
        .all()
    )
    through, expected, count, ready, saved_events = None, job["start_block"], 0, 0, 0
    continuous = True
    for r in rows:
        staged = bool(r["complete"]) and r["collected_end"] == r["end_block"]
        collected = r["status"] == "done" or staged
        if r["status"] != "done" and r["collected_end"] == r["end_block"]:
            saved_events += r["event_count"] or 0
        if r["status"] != "done" and staged:
            count += r["event_count"]
            ready += 1
        if continuous and collected and r["start_block"] == expected:
            through, expected = r["end_block"], r["end_block"] + 1
        else:
            continuous = False
    return {
        "primary_source": "alchemy",
        "collected_through": through,
        "pending_events_saved": saved_events,
        "awaiting_verification_windows": ready,
        "awaiting_verification_events": count,
        "awaiting_admission_windows": ready,
        "awaiting_admission_events": count,
    }


def next_collection_window(engine, job_id, head):
    with engine.connect() as c:
        row = (
            c.execute(
                sa.select(windows)
                .outerjoin(
                    ingestion,
                    (windows.c.job_id == ingestion.c.job_id)
                    & (windows.c.start_block == ingestion.c.start_block),
                )
                .where(
                    windows.c.job_id == job_id,
                    windows.c.status != "done",
                    windows.c.end_block <= head,
                    (
                        windows.c.error_type.is_(None)
                        | windows.c.error_type.not_in(QUARANTINE)
                    ),
                    ingestion.c.complete.is_(None)
                    | ingestion.c.complete.is_(False)
                    | (ingestion.c.end_block != windows.c.end_block),
                )
                .order_by(windows.c.start_block)
                .limit(1)
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None
