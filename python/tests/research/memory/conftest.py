"""Memory 真实 PostgreSQL 验证夹具，仅作用于显式启用的测试库。

不自动迁移或清空表；迁移由本步命令单独执行。每例只创建随机测试账号，
结束时按精确 UUID 删除该账号，由外键级联清理自己的会话样例。
"""

import os
from uuid import uuid4

import pytest
import sqlalchemy as sa

from chainscope_research.memory.contracts.common import MemoryUserContext
from chainscope_research.memory.storage.conversations import ConversationStore
from chainscope_research.memory.storage.database import engine_for, require_identity
from webapp.core.settings import load_profile


@pytest.fixture(scope="module")
def memory_engines():
    """验证专用测试 profile 和迁移水位，返回两个有界连接池；不连接开发库。"""
    if os.environ.get("CHAINSCOPE_TEST_POSTGRES") != "1":
        pytest.skip(
            "requires CHAINSCOPE_TEST_POSTGRES=1 and the dedicated test database"
        )
    profile = load_profile("test")
    migrator = engine_for(profile, "migrator")
    runtime = engine_for(profile)
    try:
        with migrator.connect() as connection:
            require_identity(connection, profile, "migrator")
            assert (
                connection.scalar(
                    sa.text("SELECT version_num FROM research.memory_alembic_version")
                )
                == "mem0001"
            )
        with runtime.connect() as connection:
            assert not require_identity(connection, profile)["can_create"]
        yield migrator, runtime
    finally:
        runtime.dispose()
        migrator.dispose()


@pytest.fixture
def memory_store(memory_engines):
    """为每例创建两个虚构账号，提供存储和上下文，结束时仅清理本例用户。"""
    migrator, runtime = memory_engines
    ids = (uuid4(), uuid4())
    with migrator.begin() as connection:
        for user_id in ids:
            email = f"memory-{user_id.hex}@example.invalid"
            connection.execute(
                sa.text("""
                INSERT INTO public.app_users
                    (user_id,email,email_key,password_hash,email_verified_at,password_changed_at)
                VALUES (:user_id,:email,:email,'memory-test-only-not-a-login-hash',clock_timestamp(),clock_timestamp())
            """),
                {"user_id": user_id, "email": email},
            )
    try:
        yield (
            ConversationStore(runtime),
            MemoryUserContext(user_id=ids[0], memory_generation=0),
            MemoryUserContext(user_id=ids[1], memory_generation=0),
            migrator,
        )
    finally:
        with migrator.begin() as connection:
            connection.execute(
                sa.text("DELETE FROM public.app_users WHERE user_id IN (:a,:b)"),
                {"a": ids[0], "b": ids[1]},
            )
