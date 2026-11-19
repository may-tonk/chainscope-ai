"""网站后端测试共用的准备工作（pytest 会自动加载本文件）。

真实数据库测试只在设置了 CHAINSCOPE_TEST_POSTGRES=1、并且 .env 填好 WEBAPP_TEST_DB_* 时运行，
否则用到 test_db 的测试会自动跳过。只使用测试库 chainscope_app_test。
"""

import os
import secrets

import pytest
import sqlalchemy as sa
from alembic import command

from webapp.core.keys import AuthKeys
from webapp.core.settings import load_profile
from webapp.db.database import engine_for, migration_config
from webapp.db.tables import TABLES

OWNED = {*TABLES, "alembic_version"}


@pytest.fixture(scope="module")
def account_memory_dependency():
    """账户重建测试先处理 Memory 用户外键，模块结束后恢复原有空表环境。

    只处理测试库中已知版本且完全为空的 Memory 表；任何业务行或未知版本都拒绝清理。
    账户迁移本身不修改，回退顺序按 Memory → 账户，恢复顺序按账户 → Memory。
    """
    if os.environ.get("CHAINSCOPE_TEST_POSTGRES") != "1":
        yield
        return
    from chainscope_research.memory.storage.database import (
        migration_config as memory_config,
    )

    profile = load_profile("test")
    migrator = engine_for(profile, "migrator")
    revision = None
    try:
        with migrator.connect() as connection:
            if sa.inspect(connection).has_table(
                "memory_alembic_version", schema="research"
            ):
                revision = connection.scalar(
                    sa.text("SELECT version_num FROM research.memory_alembic_version")
                )
            if revision:
                if revision != "mem0001":
                    pytest.fail("测试库 Memory 版本超出账户夹具已知范围，未清理")
                for table in (
                    "conversations",
                    "conversation_turns",
                    "conversation_messages",
                    "conversation_summaries",
                ):
                    if connection.scalar(
                        sa.text(f"SELECT count(*) FROM research.{table}")
                    ):
                        pytest.fail("测试库 Memory 表含数据，拒绝账户重建测试")
        if revision:
            command.downgrade(memory_config("test"), "base")
        try:
            yield
        finally:
            if revision:
                command.upgrade(migration_config("test"), "head")
                command.upgrade(memory_config("test"), revision)
    finally:
        migrator.dispose()


@pytest.fixture
def test_db(account_memory_dependency):
    """真实测试库：先确认库里没有别的表，再回退为空、升级到最新；测试结束后再回退为空。

    返回 (配置, 迁移账号连接池, 运行账号连接池)。业务代码只用运行账号。
    """
    if os.environ.get("CHAINSCOPE_TEST_POSTGRES") != "1":
        pytest.skip(
            "requires the PostgreSQL test database and CHAINSCOPE_TEST_POSTGRES=1"
        )
    profile = load_profile("test")
    migrator = engine_for(profile, "migrator")
    runtime = engine_for(profile, "runtime")
    try:
        with migrator.connect() as connection:
            existing = set(sa.inspect(connection).get_table_names())
        unknown = existing - OWNED
        if unknown:
            pytest.fail(f"测试库里有不属于账户迁移的表，未改动：{sorted(unknown)}")
        config = migration_config("test")
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        yield profile, migrator, runtime
        command.downgrade(config, "base")
    finally:
        migrator.dispose()
        runtime.dispose()


@pytest.fixture
def keys():
    """测试专用的 4 把随机密钥（每个测试都不同，不读 .env）。"""
    return AuthKeys(*(secrets.token_bytes(32) for _ in range(4)))


@pytest.fixture
def engine(test_db):
    """业务代码使用的运行账号连接池。"""
    return test_db[2]
