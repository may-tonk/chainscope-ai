"""MySQL connection, migration and transactional idempotent persistence."""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import SQLAlchemyError

from .config import ROOT, Settings
from .errors import ChainScopeError
from .models import batch_events, batches, transfers


def engine_for(settings: Settings, *, server_only=False):
    return sa.create_engine(
        settings.database_url(server_only=server_only),
        pool_pre_ping=True,
        echo=False,
        hide_parameters=True,
        connect_args={"connect_timeout": 5, "read_timeout": 30, "write_timeout": 30},
    )


def database_error(exc: SQLAlchemyError) -> ChainScopeError:
    original = getattr(exc, "orig", None)
    args = getattr(original, "args", ())
    code = args[0] if args and isinstance(args[0], int) else "unknown"
    return ChainScopeError(
        f"MySQL 操作失败（错误码 {code}）；请检查 .env、权限和迁移状态"
    )


def check_database(engine):
    with engine.connect() as connection:
        version = connection.scalar(sa.text("SELECT VERSION()"))
        tables = sa.inspect(connection).get_table_names()
        revision = (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            if "alembic_version" in tables
            else None
        )
    return {
        "mysql_version": version,
        "migration_revision": revision,
        "schema_ready": revision == "0002"
        and all(
            t in tables
            for t in (
                "collection_batches",
                "transfer_events",
                "batch_events",
                "alembic_version",
            )
        ),
    }


def initialize(settings: Settings) -> dict:
    server = engine_for(settings, server_only=True)
    try:
        with server.connect() as connection:
            version = connection.scalar(sa.text("SELECT VERSION()"))
            # Identifier validated in config; creating only the designated database.
            if "MariaDB" in version or tuple(
                map(int, version.split("-")[0].split(".")[:3])
            ) < (8, 0, 16):
                raise ChainScopeError(
                    "本项目要求 MySQL >= 8.0.16，以执行数据库 CHECK 约束"
                )
            connection.execute(
                sa.text(
                    f"CREATE DATABASE IF NOT EXISTS `{settings.mysql_database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"
                )
            )
        engine = engine_for(settings)
        try:
            with engine.begin() as connection:
                config = Config(str(ROOT / "alembic.ini"))
                config.set_main_option(
                    "script_location", str(ROOT / "python" / "migration_env")
                )
                config.set_main_option(
                    "version_locations", str(ROOT / "python" / "migrations")
                )
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
            return {"database": settings.mysql_database, **check_database(engine)}
        finally:
            engine.dispose()
    finally:
        server.dispose()


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


def start_batch(engine, batch_id, token, start, end, directory):
    with engine.begin() as connection:
        connection.execute(
            batches.insert().values(
                batch_id=batch_id,
                chain_id=56,
                token_address=token,
                start_block=start,
                end_block=end,
                provider="alchemy",
                status="running",
                started_at_utc=utcnow(),
                raw_directory=str(directory) if directory is not None else None,
            )
        )


def fail_batch(engine, batch_id, error_type):
    with engine.begin() as connection:
        connection.execute(
            batches.update()
            .where(batches.c.batch_id == batch_id, batches.c.status == "running")
            .values(
                status="failed",
                finished_at_utc=utcnow(),
                error_type=error_type[:64],
            )
        )


def store_batch(engine, batch_id: str, events: list[dict], manifest: dict) -> dict:
    inserted = 0
    with engine.begin() as connection:
        # Lock this batch; the event unique key rejects concurrent conflicting inserts.
        connection.execute(
            sa.select(batches.c.batch_id)
            .where(batches.c.batch_id == batch_id)
            .with_for_update()
        ).scalar_one()
        for event in events:
            existing = (
                connection.execute(
                    sa.select(transfers).where(
                        transfers.c.chain_id == event["chain_id"],
                        transfers.c.tx_hash == event["tx_hash"],
                        transfers.c.log_index == event["log_index"],
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if any(existing[field] != value for field, value in event.items()):
                    raise ChainScopeError(
                        "数据库同一事件内容冲突；本批次已回滚，需调查链重组或数据来源"
                    )
                event_id = existing["event_id"]
            else:
                event_id = connection.execute(
                    transfers.insert().values(**event)
                ).inserted_primary_key[0]
                inserted += 1
            connection.execute(
                batch_events.insert().values(batch_id=batch_id, event_id=event_id)
            )
        counts = {"inserted_count": inserted, "existing_count": len(events) - inserted}
        connection.execute(
            batches.update()
            .where(batches.c.batch_id == batch_id)
            .values(
                status="succeeded",
                finished_at_utc=utcnow(),
                end_block_hash=manifest["end_block_hash"],
                event_count=len(events),
                receipt_verified_events=manifest["receipt_verified_events"],
                **counts,
            )
        )
    return counts


def verify_batch(engine, batch_id: str) -> dict:
    with engine.connect() as connection:
        batch = (
            connection.execute(sa.select(batches).where(batches.c.batch_id == batch_id))
            .mappings()
            .one_or_none()
        )
        if batch is None:
            raise ChainScopeError("数据库中没有这个采集批次")
        events = (
            connection.execute(
                sa.select(transfers)
                .join(batch_events)
                .where(batch_events.c.batch_id == batch_id)
            )
            .mappings()
            .all()
        )
    count = len(events)
    if batch["status"] != "succeeded" or count != batch["event_count"]:
        raise ChainScopeError("批次未成功或事件数量与批次记录不一致")
    # Python int provides exact addition for all uint256 values; SQL SUM would coerce strings.
    return {
        "batch_id": batch_id,
        "status": batch["status"],
        "event_count": count,
        "inserted_count": batch["inserted_count"],
        "existing_count": batch["existing_count"],
        "receipt_verified_events": batch["receipt_verified_events"],
        "amount_raw_sum": str(sum(int(event["amount_raw"]) for event in events)),
        "amount_sum_unit": "raw token units; one token per batch",
    }
