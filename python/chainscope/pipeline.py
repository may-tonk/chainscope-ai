"""Orchestrate capture and persistence without hiding failed batches."""

from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from .archive import DEFAULT_BATCH_LIMIT, TOTAL_ARCHIVE_LIMIT
from .collector import capture
from .config import ROOT, Settings
from .database import (
    check_database,
    engine_for,
    fail_batch,
    start_batch,
    store_batch,
    verify_batch,
)
from .errors import ChainScopeError
from .parser import ADDRESS, fixed_hex
from .rpc_client import RpcClient


def run_collection(
    settings: Settings,
    token,
    start,
    end,
    *,
    persist,
    max_pages,
    receipt_limit,
    save_raw: bool = False,
    archive_max_bytes: int = DEFAULT_BATCH_LIMIT,
):
    token = fixed_hex(token, ADDRESS, "token")
    if (
        not 0 <= start <= end < 2**64
        or not 1 <= max_pages <= 100
        or not 1 <= receipt_limit <= 100
        or not 0 < archive_max_bytes <= TOTAL_ARCHIVE_LIMIT
    ):
        raise ChainScopeError("区块范围、分页上限、回执核验上限或归档容量不合法")
    url = settings.require_rpc()
    batch_id = str(uuid4())
    directory = ROOT / "raw" / batch_id if save_raw or not persist else None
    engine = engine_for(settings) if persist else None
    started = False
    committed = False
    try:
        if engine is not None:
            if not check_database(engine)["schema_ready"]:
                raise ChainScopeError(
                    "请先执行 python -m chainscope init-db 创建版本化表结构"
                )
            start_batch(engine, batch_id, token, start, end, directory)
            started = True
        with RpcClient(url) as rpc:
            events, manifest = capture(
                rpc,
                token,
                start,
                end,
                directory,
                batch_id=batch_id,
                max_pages=max_pages,
                receipt_limit=receipt_limit,
                archive_max_bytes=archive_max_bytes,
            )
        if engine is not None:
            counts = store_batch(engine, batch_id, events, manifest)
            committed = True
            # Verify database contents by reading back after commit.
            verification = verify_batch(engine, batch_id)
            manifest.update(
                status="succeeded", **counts, database_verification=verification
            )
            if rpc.archive is not None:
                rpc.archive.write_json("manifest.json", manifest)
        return {
            **manifest,
            "raw_directory": str(directory) if directory is not None else None,
        }
    except (
        ChainScopeError,
        SQLAlchemyError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
    ) as exc:
        if committed:
            raise ChainScopeError(
                f"批次 {batch_id} 已提交 MySQL，但提交后的核对或文件更新失败，请执行 verify 检查"
            ) from None
        if engine is not None and started:
            try:
                fail_batch(engine, batch_id, type(exc).__name__)
            except SQLAlchemyError:
                # A disconnected database can leave 'running'; never claim success.
                raise ChainScopeError(
                    f"批次 {batch_id} 失败，且失败状态未能写回 MySQL，请核对 running 批次"
                ) from None
        raise
    finally:
        if engine is not None:
            engine.dispose()
