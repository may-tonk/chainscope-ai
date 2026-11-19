"""Memory 的 PostgreSQL 连接与迁移身份检查，不在导入时连接或建表。

连接 profile 由装配方注入，沿用现有 TLS 与运行/迁移账号配置；业务存储不导入网站。
所有 Memory 业务表显式位于 research，版本表独立于账户和 checkpoint 迁移。
"""

from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config

from chainscope_research.memory.settings import load_memory_settings

ROOT = Path(__file__).resolve().parents[4]
SCHEMA = "research"
VERSION_TABLE = "memory_alembic_version"


def engine_for(profile, role="runtime", *, settings=None):
    """根据注入 profile 创建有界连接池，调用方负责 dispose；此时尚未建立连接。

    数据库等待有时间上限，参数不进入异常日志；默认 schema 不改变账户库配置。
    """
    limits = (settings or load_memory_settings()).database
    return sa.create_engine(
        profile.url(role),
        connect_args={
            **profile.connect_args(),
            "application_name": f"chainscope-memory-{profile.name}",
            "connect_timeout": limits.connect_timeout_seconds,
            "options": f"-c search_path=research,public -c statement_timeout={limits.statement_timeout_ms} -c lock_timeout={limits.lock_timeout_ms}",
        },
        pool_size=limits.pool_size,
        max_overflow=limits.max_overflow,
        pool_timeout=limits.pool_timeout_seconds,
        pool_pre_ping=True,
        hide_parameters=True,
    )


def require_identity(connection, profile, role="runtime") -> dict:
    """读取真实库、账号、TLS 和 schema 权限；不符合用途时在写入前拒绝。

    迁移还要求拥有 research 且账户表已经存在；运行账号必须没有 schema CREATE。
    返回安全状态字典，不包含密码和连接字符串。
    """
    found = dict(
        connection.execute(
            sa.text("""
        SELECT current_database() AS database, current_user AS account,
            (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AS superuser,
            (SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()) AS tls,
            has_schema_privilege(current_user, 'research', 'USAGE') AS can_use,
            has_schema_privilege(current_user, 'research', 'CREATE') AS can_create,
            (SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname='research') AS owner,
            to_regclass('public.app_users') IS NOT NULL AS accounts_ready
    """)
        )
        .mappings()
        .one()
    )
    valid = (
        found["database"] == profile.database
        and found["account"] == profile.account(role).user
        and found["tls"]
        and not found["superuser"]
        and found["can_use"]
        and found["accounts_ready"]
    )
    if role == "migrator":
        valid = (
            valid and found["can_create"] and found["owner"] == profile.migrator.user
        )
    elif role == "runtime":
        valid = valid and not found["can_create"]
    else:
        valid = False
    if not valid:
        raise ValueError("Memory 数据库身份、TLS、账户表或 schema 权限不符合要求")
    return found


def migration_config(profile_name="development") -> Config:
    """返回 Memory 独立 Alembic 配置；构造配置不执行迁移。"""
    config = Config(str(ROOT / "alembic.ini"), ini_section="memory")
    config.attributes["profile"] = profile_name
    return config
