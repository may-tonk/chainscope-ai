"""登录、会话、退出、改密码、注销的测试（真实测试库）。"""

from dataclasses import replace

import pytest
import sqlalchemy as sa

from webapp.core.errors import AuthenticationFailed, PasswordRejected, RateLimited
from webapp.db.tables import events, sessions, users
from webapp.security import passwords
from webapp.services import accounts, rate_limits
from webapp.services import sessions as session_service

EMAIL = "Owner@Example.com"
PASSWORD = "river tea lamp orbit 47"
NEW_PASSWORD = "seven blue kites over hills"
OWNER = "203.0.113.5"
ATTACKER = "198.51.100.7"


def make_user(engine, email=EMAIL, password=PASSWORD, status="active"):
    """直接在库里建一个账户（注册流程另有测试，这里只为准备数据）。"""
    with engine.begin() as connection:
        return connection.scalar(
            users.insert()
            .values(
                email=email,
                email_key=email.lower(),
                password_hash=passwords.hash_password(password),
                email_verified_at=sa.func.now(),
                password_changed_at=sa.func.now(),
                status=status,
            )
            .returning(users.c.user_id)
        )


def login(engine, keys, email=EMAIL, password=PASSWORD, source=OWNER):
    return accounts.login(engine, keys, email, password, source=source)


def event_reasons(engine, event_type):
    with engine.begin() as connection:
        return connection.scalars(
            sa.select(events.c.reason).where(events.c.event_type == event_type)
        ).all()


def test_login_issues_a_session_that_lookup_accepts(engine, keys):
    """登录成功：返回新会话，查验通过；数据库只存令牌指纹，不存原值。"""
    user_id = make_user(engine)
    result = login(engine, keys, email="  owner@EXAMPLE.com ")
    assert result.user_id == user_id
    info = session_service.lookup(engine, keys, result.session.token)
    assert (info.kind, info.user_id) == ("user", user_id)
    assert session_service.csrf_matches(keys, info, result.session.csrf_token)
    assert not session_service.csrf_matches(keys, info, "wrong")
    with engine.begin() as connection:
        stored = connection.scalar(sa.select(sessions.c.token_hash))
    assert result.session.token.encode() not in stored
    assert session_service.lookup(engine, keys, "not-a-token") is None
    assert session_service.lookup(engine, keys, None) is None


def test_all_login_failures_look_the_same(engine, keys):
    """邮箱不存在、密码错误、账户被禁用：对外是同一个错误和提示（验收 A01），原因只在审计里。"""
    make_user(engine)
    make_user(engine, email="off@example.com", status="disabled")
    messages = []
    for email, password in [
        ("nobody@example.com", PASSWORD),
        (EMAIL, "wrong password value"),
        ("off@example.com", PASSWORD),
    ]:
        with pytest.raises(AuthenticationFailed) as caught:
            login(engine, keys, email=email, password=password)
        messages.append(str(caught.value))
    assert messages == ["账号或密码错误"] * 3
    assert sorted(event_reasons(engine, "login_failed")) == [
        "disabled",
        "unknown_email",
        "wrong_password",
    ]


def test_attacker_cannot_lock_out_the_owner(engine, keys):
    """攻击者从自己的来源连续输错 5 次后被冷却；真实用户从自己的来源仍能正常登录（验收 A20）。"""
    make_user(engine)
    for _ in range(5):
        with pytest.raises(AuthenticationFailed):
            login(engine, keys, password="wrong password value", source=ATTACKER)
    with pytest.raises(RateLimited):
        login(engine, keys, source=ATTACKER)  # 即使密码正确，冷却期内也不处理
    assert login(engine, keys, source=OWNER)


def test_source_limit_counts_every_attempt(engine, keys):
    """同一来源每分钟最多 20 次登录尝试，成功的也算。"""
    make_user(engine)
    for _ in range(rate_limits.LOGIN_SOURCE.limit):
        login(engine, keys)
    with pytest.raises(RateLimited):
        login(engine, keys)


def test_logout_revokes_only_the_current_session(engine, keys):
    """退出当前设备：这个令牌立即失效（复制走的也不能用），其他设备不受影响（验收 A05）。"""
    user_id = make_user(engine)
    phone = login(engine, keys).session
    laptop = login(engine, keys).session
    accounts.logout(engine, phone.session_id, user_id)
    assert session_service.lookup(engine, keys, phone.token) is None
    assert session_service.lookup(engine, keys, laptop.token) is not None


def test_logout_everywhere_needs_the_current_password(engine, keys):
    """退出所有设备：当前密码错误时拒绝；正确时全部会话失效。"""
    user_id = make_user(engine)
    first = login(engine, keys).session
    second = login(engine, keys).session
    with pytest.raises(AuthenticationFailed):
        accounts.logout_everywhere(
            engine, keys, user_id, "wrong password value", source=OWNER
        )
    assert session_service.lookup(engine, keys, first.token) is not None
    accounts.logout_everywhere(engine, keys, user_id, PASSWORD, source=OWNER)
    assert session_service.lookup(engine, keys, first.token) is None
    assert session_service.lookup(engine, keys, second.token) is None


def test_change_password_revokes_old_sessions_and_old_password(engine, keys):
    """改密码：旧会话和旧密码都失效，新密码可以登录（验收 A15）；新密码同样要符合规则。"""
    user_id = make_user(engine)
    old = login(engine, keys).session
    with pytest.raises(PasswordRejected):
        accounts.change_password(engine, keys, user_id, PASSWORD, "short", source=OWNER)
    with pytest.raises(AuthenticationFailed):
        accounts.change_password(
            engine, keys, user_id, "wrong password value", NEW_PASSWORD, source=OWNER
        )
    accounts.change_password(
        engine, keys, user_id, PASSWORD, NEW_PASSWORD, source=OWNER
    )
    assert session_service.lookup(engine, keys, old.token) is None
    with pytest.raises(AuthenticationFailed):
        login(engine, keys)
    assert login(engine, keys, password=NEW_PASSWORD)


def test_wrong_current_password_is_rate_limited(engine, keys):
    """已登录后连续 5 次输错当前密码，冷却 15 分钟。"""
    user_id = make_user(engine)
    for _ in range(5):
        with pytest.raises(AuthenticationFailed):
            accounts.logout_everywhere(
                engine, keys, user_id, "wrong password value", source=OWNER
            )
    with pytest.raises(RateLimited):
        accounts.logout_everywhere(engine, keys, user_id, PASSWORD, source=OWNER)


def test_password_changed_during_login_does_not_create_a_session(
    engine, keys, monkeypatch
):
    """旧密码刚校验通过、另一个请求却改了密码：这次登录被拒绝，不会用旧密码换到新会话。"""
    user_id = make_user(engine)
    real_verify = accounts.verify_password

    def verify_then_change(password, stored):
        outcome = real_verify(password, stored)
        with engine.begin() as connection:  # 模拟并发的改密码请求
            connection.execute(
                users.update()
                .where(users.c.user_id == user_id)
                .values(
                    password_hash=passwords.hash_password(NEW_PASSWORD),
                    auth_version=users.c.auth_version + 1,
                )
            )
        return outcome

    monkeypatch.setattr(accounts, "verify_password", verify_then_change)
    with pytest.raises(AuthenticationFailed):
        login(engine, keys)
    with engine.begin() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(sessions)) == 0
    assert event_reasons(engine, "login_failed") == ["changed_during_login"]


def test_disabled_account_and_version_change_end_sessions(engine, keys):
    """账户被禁用、或认证版本变化后，已有会话立即无效（不只是前端隐藏入口）。"""
    user_id = make_user(engine)
    session = login(engine, keys).session
    with engine.begin() as connection:
        connection.execute(users.update().values(status="disabled"))
    assert session_service.lookup(engine, keys, session.token) is None
    with engine.begin() as connection:
        connection.execute(users.update().values(status="active"))
    assert session_service.lookup(engine, keys, session.token) is not None
    with engine.begin() as connection:
        connection.execute(
            users.update()
            .where(users.c.user_id == user_id)
            .values(auth_version=users.c.auth_version + 1)
        )
    assert session_service.lookup(engine, keys, session.token) is None


def test_sessions_expire_on_idle_absolute_time_and_key_change(engine, keys):
    """24 小时无操作、7 天到期、或换了密钥版本，会话都会失效。"""
    make_user(engine)
    idle = login(engine, keys).session
    old = login(engine, keys).session
    rotated = login(engine, keys).session
    with engine.begin() as connection:
        connection.execute(
            sessions.update()
            .where(sessions.c.session_id == idle.session_id)
            .values(last_seen_at=sa.func.now() - session_service.USER_IDLE)
        )
        connection.execute(
            sessions.update()
            .where(sessions.c.session_id == old.session_id)
            .values(absolute_expires_at=sa.func.now())
        )
    assert session_service.lookup(engine, keys, idle.token) is None
    assert session_service.lookup(engine, keys, old.token) is None
    assert session_service.lookup(engine, keys, rotated.token) is not None
    assert (
        session_service.lookup(engine, replace(keys, version=2), rotated.token) is None
    )


def test_last_seen_is_refreshed_only_after_a_while(engine, keys):
    """最近活动时间不是每次都写：5 分钟内重复访问不更新，超过才更新。"""
    make_user(engine)
    session = login(engine, keys).session

    def last_seen():
        with engine.begin() as connection:
            return connection.scalar(sa.select(sessions.c.last_seen_at))

    first = last_seen()
    session_service.lookup(engine, keys, session.token)
    assert last_seen() == first
    with engine.begin() as connection:
        connection.execute(
            sessions.update().values(
                last_seen_at=sa.func.now() - session_service.TOUCH_EVERY
            )
        )
    stale = last_seen()
    session_service.lookup(engine, keys, session.token)
    assert last_seen() > stale


def test_delete_account_removes_everything_and_allows_reregistration(engine, keys):
    """注销账户：需要当前密码；成功后会话全部消失、不能再登录，审计里不留用户编号；
    同一个邮箱可以重新建立新账户，不继承旧数据（验收 A19）。"""
    user_id = make_user(engine)
    session = login(engine, keys).session
    with pytest.raises(AuthenticationFailed):
        accounts.delete_account(
            engine, keys, user_id, "wrong password value", source=OWNER
        )
    accounts.delete_account(engine, keys, user_id, PASSWORD, source=OWNER)
    assert session_service.lookup(engine, keys, session.token) is None
    assert accounts.get_account(engine, user_id) is None
    with pytest.raises(AuthenticationFailed):
        login(engine, keys)
    with engine.begin() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(sessions)) == 0
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(events)
                .where(events.c.user_id.is_not(None))
            )
            == 0
        )
    new_id = make_user(engine)
    assert new_id != user_id


def test_get_account_returns_display_fields_only(engine, keys):
    """账户页面只拿到编号、邮箱和创建时间，拿不到密码哈希。"""
    user_id = make_user(engine)
    account = accounts.get_account(engine, user_id)
    assert (account.user_id, account.email) == (user_id, EMAIL)
    assert not hasattr(account, "password_hash")


def test_database_outage_never_lets_a_request_through(keys, test_db):
    """账户库连不上时，查验会话直接报错，而不是当作“已登录”放行（验收 A10）。"""
    profile = test_db[0]
    broken = sa.create_engine(
        profile.url("runtime").set(port=1),
        connect_args={**profile.connect_args(), "connect_timeout": 2},
    )
    try:
        with pytest.raises(sa.exc.OperationalError):
            session_service.lookup(broken, keys, "some-token-value")
        with pytest.raises(sa.exc.OperationalError):
            login(broken, keys)
    finally:
        broken.dispose()
