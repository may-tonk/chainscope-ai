"""账户库迁移的测试。

分两类：
- 不连数据库的测试：每次都运行；
- 真实数据库测试（带 @requires_postgres）：只在设置了 CHAINSCOPE_TEST_POSTGRES=1、
  并且 .env 填好 WEBAPP_TEST_DB_* 时运行。只使用测试库 chainscope_app_test；
  库里有不属于账户迁移的表时直接停下、不做任何改动；结束后把测试库回退为空。
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import DBAPIError

from webapp.core.errors import MigrationRefused
from webapp.core.settings import load_profile
from webapp.db import database
from webapp.db.database import migration_config, require_migrator
from webapp.db.tables import TABLES, metadata

requires_postgres = pytest.mark.skipif(
    os.environ.get("CHAINSCOPE_TEST_POSTGRES") != "1",
    reason="requires the PostgreSQL test database and CHAINSCOPE_TEST_POSTGRES=1",
)
OWNED = {*TABLES, "alembic_version"}
HEAD = "0002"  # 账户库当前最新的迁移版本


def sqlstate(exc: DBAPIError) -> str:
    """取出 PostgreSQL 的标准错误编号，例如 23505 = 违反唯一约束。"""
    return exc.orig.sqlstate


def fake_identity(**changes):
    """伪造一份“服务器回答的身份信息”，默认全部合格，再按参数改坏某一项。"""
    found = {
        "account": "chainscope_test_migrator",
        "database": "chainscope_app_test",
        "schema": "public",
        "can_use": True,
        "can_create": True,
        "superuser": False,
        "tls": True,
    }
    return {**found, **changes}


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"account": "postgres", "superuser": True}, "超级管理员"),
        ({"account": "chainscope_test"}, "应为 chainscope_test_migrator"),
        ({"database": "chainscope_app"}, "应为 chainscope_app_test"),
        ({"can_create": False}, "CREATE"),
        ({"tls": False}, "TLS"),
    ],
)
def test_migration_refuses_the_wrong_connection(
    monkeypatch, tmp_path, changes, message
):
    """不连数据库：身份信息任何一项不对（超级管理员、运行账号、开发库、无建表权限、未加密），都拒绝迁移。"""
    cert = tmp_path / "ca.crt"
    cert.write_text("test")
    profile = load_profile(
        "test",
        {
            "WEBAPP_DB_HOST": "192.0.2.10",
            "WEBAPP_DB_SSLROOTCERT": str(cert),
            "WEBAPP_TEST_DB_NAME": "chainscope_app_test",
            "WEBAPP_TEST_DB_USER": "chainscope_test",
            "WEBAPP_TEST_DB_MIGRATOR_USER": "chainscope_test_migrator",
        },
    )
    monkeypatch.setattr(database, "identity", lambda _: fake_identity(**changes))
    with pytest.raises(MigrationRefused, match=message):
        require_migrator(None, profile)


@requires_postgres
def test_migration_matches_tables_and_can_run_again(test_db):
    """迁移建出的表和 tables.py 的定义完全一致；已经是最新版本时再升级一次不会出错。"""
    profile, migrator, _ = test_db
    with migrator.connect() as connection:
        assert set(sa.inspect(connection).get_table_names()) == OWNED
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == HEAD
        )
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        diffs = [
            d
            for d in compare_metadata(context, metadata)
            if not (d[0] == "remove_table" and d[1].name == "alembic_version")
        ]
        assert diffs == []
    command.upgrade(migration_config("test"), "head")  # already at head: no-op


def fresh_state(profile) -> tuple[set, str | None]:
    """用一条全新的连接（不走任何连接池）读取：库里有哪些表、迁移版本是多少。

    用运行账号读：别的账号的新连接只能看到已经提交的数据，
    所以这里看到的结果就证明迁移已经真正提交、持久化了。
    """
    engine = sa.create_engine(
        profile.url("runtime"),
        connect_args=profile.connect_args(),
        poolclass=sa.pool.NullPool,
    )
    try:
        with engine.connect() as connection:
            tables = set(sa.inspect(connection).get_table_names())
            version = (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                if "alembic_version" in tables
                else None
            )
        return tables, version
    finally:
        engine.dispose()


@requires_postgres
def test_upgrade_and_downgrade_are_committed(test_db):
    """命令行方式（env.py 自己建连接）的升级和回退都真正提交：另一条新连接能看到结果。"""
    profile, _, _ = test_db
    assert fresh_state(profile) == (OWNED, HEAD)
    config = migration_config("test")
    command.downgrade(config, "base")
    # 回退后只剩 alembic_version 这张空的版本表。
    assert fresh_state(profile) == ({"alembic_version"}, None)
    command.upgrade(config, "head")
    assert fresh_state(profile) == (OWNED, HEAD)


@requires_postgres
def test_caller_owns_the_transaction_of_a_passed_in_connection(test_db):
    """外部传入连接时，事务归调用方：没开事务就拒绝；调用方回滚，迁移就不生效。"""
    profile, migrator, _ = test_db
    config = migration_config("test")
    with migrator.connect() as connection:
        config.attributes["connection"] = connection
        with pytest.raises(MigrationRefused, match="调用方开启事务"):
            command.downgrade(config, "base")
    with migrator.connect() as connection:
        with connection.begin() as transaction:
            config.attributes["connection"] = connection
            command.downgrade(config, "base")
            transaction.rollback()  # 调用方决定不提交
    assert fresh_state(profile) == (OWNED, HEAD)


@requires_postgres
def test_runtime_account_reads_and_writes_but_cannot_create_tables(test_db):
    """运行账号能读写新表（数据库端默认权限生效），但建表会因权限不足（42501）被拒绝。"""
    _, _, runtime = test_db
    now = datetime.now(UTC)
    # 身份查询单独用一条连接：查询会自动开始事务（autobegin），
    # 如果接着在同一条连接上 connection.begin()，SQLAlchemy 会报“事务已经开始”。
    with runtime.connect() as connection:
        found = database.identity(connection)
    assert found["tls"] and not found["can_create"] and not found["superuser"]
    with runtime.connect() as connection:
        with connection.begin():
            user_id = connection.scalar(
                sa.text(
                    "INSERT INTO app_users (email, email_key, password_hash,"
                    " email_verified_at, password_changed_at)"
                    " VALUES ('A@example.com', 'a@example.com', 'h', :t, :t)"
                    " RETURNING user_id"
                ),
                {"t": now},
            )
            assert isinstance(user_id, uuid.UUID)
            event = connection.scalar(
                sa.text(
                    "INSERT INTO auth_events (user_id, event_type)"
                    " VALUES (:u, 'test') RETURNING event_id"
                ),
                {"u": user_id},
            )
            assert event >= 1  # identity sequence usable by the runtime account
            connection.execute(sa.text("DELETE FROM app_users"))
        with pytest.raises(DBAPIError) as caught:
            with connection.begin():
                connection.execute(sa.text("CREATE TABLE should_not_exist (x int)"))
        assert sqlstate(caught.value) == "42501"  # insufficient_privilege


@requires_postgres
def test_constraints_protect_account_rules(test_db):
    """数据库约束有效：email_key 必须是小写邮箱、同一邮箱只能一个账户、已登录会话必须有用户；删除账户时会话自动删除，审计记录只清空用户编号。"""
    _, _, runtime = test_db
    now = datetime.now(UTC)
    insert_user = sa.text(
        "INSERT INTO app_users (email, email_key, password_hash,"
        " email_verified_at, password_changed_at)"
        " VALUES (:email, :key, 'h', :t, :t) RETURNING user_id"
    )

    def violates(statement, params) -> str:
        """执行一条语句并断言它违反约束，返回 PostgreSQL 的错误编号。"""
        with runtime.connect() as connection:
            with pytest.raises(DBAPIError) as caught:
                with connection.begin():
                    connection.execute(statement, params)
        return sqlstate(caught.value)

    params = {"email": "B@example.com", "key": "b@example.com", "t": now}
    assert violates(insert_user, {**params, "key": "B@example.com"}) == "23514"
    with runtime.connect() as connection, connection.begin() as transaction:
        user_id = connection.scalar(insert_user, params)
        # Same address in another case: one account only.
        with pytest.raises(DBAPIError) as caught:
            with connection.begin_nested():
                connection.execute(insert_user, {**params, "email": "b@Example.com"})
        assert sqlstate(caught.value) == "23505"
        insert_session = sa.text(
            "INSERT INTO auth_sessions (kind, token_hash, key_version, user_id,"
            " auth_version, csrf_hash, absolute_expires_at)"
            " VALUES (:kind, :token, 1, :user, :version, 'c', :expires)"
        )
        expires = now + timedelta(days=7)
        with pytest.raises(DBAPIError) as caught:
            with connection.begin_nested():
                connection.execute(
                    insert_session,
                    {
                        "kind": "user",
                        "token": b"t1",
                        "user": None,
                        "version": None,
                        "expires": expires,
                    },
                )
        assert sqlstate(caught.value) == "23514"
        connection.execute(
            insert_session,
            {
                "kind": "user",
                "token": b"t2",
                "user": user_id,
                "version": 1,
                "expires": expires,
            },
        )
        connection.execute(
            sa.text("INSERT INTO auth_events (user_id, event_type) VALUES (:u, 'x')"),
            {"u": user_id},
        )
        # Deleting an account removes its sessions and keeps anonymous audit rows.
        connection.execute(
            sa.text("DELETE FROM app_users WHERE user_id = :u"), {"u": user_id}
        )
        assert connection.scalar(sa.text("SELECT count(*) FROM auth_sessions")) == 0
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM auth_events WHERE user_id IS NULL")
            )
            == 1
        )
        transaction.rollback()


@requires_postgres
def test_migrator_check_rejects_the_runtime_account(test_db):
    """用运行账号的真实连接做迁移前检查，会被拒绝。"""
    profile, _, runtime = test_db
    with runtime.connect() as connection:
        with pytest.raises(MigrationRefused, match="应为 chainscope_test_migrator"):
            require_migrator(connection, profile)


def test_downgrade_is_refused_outside_the_test_database(capsys):
    """不连数据库：不是测试库时回退被拒绝；离线生成升级 SQL 正常。"""
    # Offline mode renders SQL only; without the test profile it must refuse.
    with pytest.raises(RuntimeError, match="只允许在测试库"):
        command.downgrade(migration_config("development"), "0001:base", sql=True)
    command.upgrade(migration_config("development"), "head", sql=True)
    assert "CREATE TABLE app_users" in capsys.readouterr().out


@requires_postgres
def test_upgrade_to_0002_keeps_existing_registration_requests(test_db):
    """从 0001 升级到 0002：已有的注册请求原样保留（user_id 为空，满足新约束）；
    新约束要求重置请求必须有用户编号。"""
    profile, migrator, _ = test_db
    config = migration_config("test")
    command.downgrade(config, "0001")
    with migrator.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO auth_challenges (email, email_key, purpose, code_hmac,"
                " key_version, flow_binding, expires_at)"
                " VALUES ('a@example.com', 'a@example.com', 'register', 'c', 1, 'f',"
                " now() + interval '5 minutes')"
            )
        )
    command.upgrade(config, "head")
    with migrator.connect() as connection:
        kept = connection.execute(
            sa.text("SELECT email, purpose, user_id FROM auth_challenges")
        ).one()
        assert tuple(kept) == ("a@example.com", "register", None)
    # 新开一条连接再显式开事务：上面的查询已经自动开始了事务（autobegin），不能接着 begin()。
    with migrator.connect() as connection:
        with pytest.raises(DBAPIError) as caught:
            with connection.begin():
                connection.execute(
                    sa.text(
                        "INSERT INTO auth_challenges (email, email_key, purpose,"
                        " code_hmac, key_version, flow_binding, expires_at)"
                        " VALUES ('b@example.com', 'b@example.com', 'password_reset',"
                        " 'c', 1, 'f', now())"
                    )
                )
        assert sqlstate(caught.value) == "23514"
    assert fresh_state(profile) == (OWNED, HEAD)
