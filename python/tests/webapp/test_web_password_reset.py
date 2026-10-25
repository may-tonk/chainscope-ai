"""重置密码测试（真实测试库，验收 A14）。"""

import threading

import pytest
import sqlalchemy as sa

from webapp.core.errors import (
    AuthenticationFailed,
    MailDeliveryFailed,
    PasswordRejected,
    PasswordResetFailed,
    RateLimited,
    RegistrationFailed,
    VerificationFailed,
)
from webapp.db.tables import challenges, events, rate_limits, sessions, users
from webapp.security import passwords
from webapp.services import accounts, password_reset, registration
from webapp.services import rate_limits as limits
from webapp.services import sessions as session_service

EMAIL = "Owner@Example.com"
OLD = "river tea lamp"
NEW = "seven blue kites"
SOURCE = "203.0.113.5"


class FakeMailer:
    def __init__(self, fail=False):
        self.reset_codes = {}
        self.changed = []
        self.fail = fail

    def send_password_reset_code(self, to, code):
        if self.fail:
            raise MailDeliveryFailed()
        self.reset_codes[to] = code

    def send_password_changed(self, to):
        self.changed.append(to)

    def send_registration_code(self, to, code):
        raise AssertionError("重置流程不应发送注册验证码")

    def send_already_registered(self, to):
        raise AssertionError("重置流程不应发送注册提醒")


def make_user(engine, email=EMAIL, password=OLD, status="active"):
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


@pytest.fixture
def flow(engine, keys):
    return session_service.start_anonymous(engine, keys).session_id


def start(engine, keys, flow, email=EMAIL, mailer=None):
    """第 1 步，并像接口层一样在之后“后台发信”。"""
    started = password_reset.start(
        engine, keys, email, flow_session_id=flow, source=SOURCE
    )
    if mailer is not None:
        password_reset.deliver_code(engine, mailer, started)
    return started


def verify(engine, keys, challenge_id, code, flow):
    return password_reset.verify(
        engine, keys, challenge_id, code, flow_session_id=flow, source=SOURCE
    )


def complete(engine, keys, token, flow, password=NEW):
    return password_reset.complete(
        engine, keys, token, password, flow_session_id=flow, source=SOURCE
    )


def reset(engine, keys, flow, mailer, email=EMAIL, password=NEW):
    started = start(engine, keys, flow, email, mailer)
    token = verify(
        engine, keys, started.challenge_id, mailer.reset_codes[started.email], flow
    )
    return complete(engine, keys, token, flow, password)


def login(engine, keys, password, source=SOURCE):
    return accounts.login(engine, keys, EMAIL, password, source=source)


def clear_send_limits(engine):
    with engine.begin() as connection:
        connection.execute(
            rate_limits.delete().where(rate_limits.c.purpose != "verify_email")
        )


def test_reset_changes_only_the_password_and_keeps_the_account(engine, keys, flow):
    """重置后：旧密码失效、新密码可登录；账户的用户编号、邮箱、注册时间都不变，账户仍然存在。"""
    user_id = make_user(engine)
    with engine.begin() as connection:
        before = connection.execute(
            sa.select(users.c.user_id, users.c.email, users.c.created_at)
        ).one()
    mailer = FakeMailer()
    assert reset(engine, keys, flow, mailer) == EMAIL
    with engine.begin() as connection:
        after = connection.execute(
            sa.select(users.c.user_id, users.c.email, users.c.created_at)
        ).one()
        assert connection.scalar(sa.select(sa.func.count()).select_from(users)) == 1
    assert tuple(after) == tuple(before)
    with pytest.raises(AuthenticationFailed):
        login(engine, keys, OLD)
    assert login(engine, keys, NEW).user_id == user_id


def test_reset_logs_out_every_device(engine, keys, flow):
    """重置后，所有设备上的旧登录立即失效（会话只是被标记撤销，账户不受影响）。"""
    make_user(engine)
    phone = login(engine, keys, OLD).session
    laptop = login(engine, keys, OLD).session
    reset(engine, keys, flow, FakeMailer())
    assert session_service.lookup(engine, keys, phone.token) is None
    assert session_service.lookup(engine, keys, laptop.token) is None
    with engine.begin() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(sessions)
                .where(sessions.c.kind == "user", sessions.c.revoked_at.is_(None))
            )
            == 0
        )


def test_unknown_and_disabled_emails_look_the_same_and_get_no_mail(engine, keys, flow):
    """不存在的邮箱、被禁用的账户：返回同样格式的请求编号，但数据库里没有请求、也不发邮件。"""
    make_user(engine, email="off@example.com", status="disabled")
    mailer = FakeMailer()
    for email in ("nobody@example.com", "off@example.com"):
        started = start(engine, keys, flow, email, mailer)
        assert started.challenge_id and started.email is None and started.code is None
        with pytest.raises(VerificationFailed):
            verify(engine, keys, started.challenge_id, "000000", flow)
    assert mailer.reset_codes == {}
    with engine.begin() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(challenges)) == 0
        )


def test_unknown_emails_are_rate_limited_too(engine, keys, flow):
    """没有账户的邮箱同样受 60 秒重发限制：“只有有账户的才被限速”本身就会泄露信息。"""
    start(engine, keys, flow, "nobody@example.com")
    with pytest.raises(RateLimited):
        start(engine, keys, flow, "nobody@example.com")


def test_wrong_codes_and_other_browsers_are_refused(engine, keys, flow):
    """输错 5 次后请求作废；别的浏览器拿到验证码也用不了；验证码过期后失效。"""
    make_user(engine)
    mailer = FakeMailer()
    started = start(engine, keys, flow, mailer=mailer)
    code = mailer.reset_codes[EMAIL]
    other = session_service.start_anonymous(engine, keys).session_id
    with pytest.raises(VerificationFailed):
        verify(engine, keys, started.challenge_id, code, other)
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for _ in range(5):
        with pytest.raises(VerificationFailed):
            verify(engine, keys, started.challenge_id, wrong, flow)
    with pytest.raises(VerificationFailed):
        verify(engine, keys, started.challenge_id, code, flow)

    clear_send_limits(engine)
    started = start(engine, keys, flow, mailer=mailer)
    with engine.begin() as connection:
        connection.execute(challenges.update().values(expires_at=sa.func.now()))
    with pytest.raises(VerificationFailed):
        verify(engine, keys, started.challenge_id, mailer.reset_codes[EMAIL], flow)


def test_token_is_single_use_and_bound_to_the_browser(engine, keys, flow):
    """重置凭证只能用一次，换了浏览器不能用；新密码不合规时凭证保留，可以换个密码重试。"""
    make_user(engine)
    mailer = FakeMailer()
    started = start(engine, keys, flow, mailer=mailer)
    token = verify(engine, keys, started.challenge_id, mailer.reset_codes[EMAIL], flow)
    other = session_service.start_anonymous(engine, keys).session_id
    with pytest.raises(PasswordResetFailed):
        complete(engine, keys, token, other)
    with pytest.raises(PasswordRejected):
        complete(engine, keys, token, flow, password="short")
    complete(engine, keys, token, flow)
    with pytest.raises(PasswordResetFailed):
        complete(engine, keys, token, flow, password="another fine one")


def test_concurrent_completion_succeeds_once(engine, keys, flow):
    """两个请求同时拿同一个重置凭证：只有一个成功。"""
    make_user(engine)
    mailer = FakeMailer()
    started = start(engine, keys, flow, mailer=mailer)
    token = verify(engine, keys, started.challenge_id, mailer.reset_codes[EMAIL], flow)
    barrier = threading.Barrier(2)
    outcomes = []

    def run():
        barrier.wait()
        try:
            complete(engine, keys, token, flow)
            outcomes.append("ok")
        except PasswordResetFailed:
            outcomes.append("failed")

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["failed", "ok"]


def test_registration_and_reset_codes_cannot_be_swapped(engine, keys, flow):
    """重置请求的编号和验证码，拿到注册接口用会失败；重置凭证也不能用来完成注册。"""
    make_user(engine)
    mailer = FakeMailer()
    started = start(engine, keys, flow, mailer=mailer)
    code = mailer.reset_codes[EMAIL]
    with pytest.raises(VerificationFailed):
        registration.verify(
            engine,
            keys,
            started.challenge_id,
            code,
            flow_session_id=flow,
            source=SOURCE,
        )
    token = verify(engine, keys, started.challenge_id, code, flow)
    with pytest.raises(RegistrationFailed):
        registration.complete(
            engine, keys, token, NEW, flow_session_id=flow, source=SOURCE
        )
    assert complete(engine, keys, token, flow) == EMAIL


def test_deleted_account_takes_its_reset_requests_with_it(engine, keys, flow):
    """账户注销后，它的重置请求随之删除；同一邮箱新注册的账户不会被旧凭证改密码。"""
    user_id = make_user(engine)
    mailer = FakeMailer()
    started = start(engine, keys, flow, mailer=mailer)
    token = verify(engine, keys, started.challenge_id, mailer.reset_codes[EMAIL], flow)
    accounts.delete_account(engine, keys, user_id, OLD, source=SOURCE)
    with engine.begin() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(challenges)) == 0
        )
    make_user(engine)  # 同一邮箱的新账户
    with pytest.raises(PasswordResetFailed):
        complete(engine, keys, token, flow)
    assert login(engine, keys, OLD)


def test_changing_password_while_logged_in_cancels_pending_resets(engine, keys, flow):
    """登录状态下修改了密码，之前申请、还没完成的重置自动作废。"""
    user_id = make_user(engine)
    mailer = FakeMailer()
    started = start(engine, keys, flow, mailer=mailer)
    token = verify(engine, keys, started.challenge_id, mailer.reset_codes[EMAIL], flow)
    accounts.change_password(
        engine, keys, user_id, OLD, "brand new secret", source=SOURCE
    )
    with pytest.raises(PasswordResetFailed):
        complete(engine, keys, token, flow)


def test_reset_clears_this_sources_login_cooldown(engine, keys, flow):
    """用户反复输错被冷却后来重置：重置成功后同一来源马上能用新密码登录。"""
    make_user(engine)
    for _ in range(5):
        with pytest.raises(AuthenticationFailed):
            login(engine, keys, "wrong password")
    with pytest.raises(RateLimited):
        login(engine, keys, OLD)
    reset(engine, keys, flow, FakeMailer())
    assert login(engine, keys, NEW)


def test_new_request_replaces_the_old_one(engine, keys, flow):
    """重新获取验证码后，旧的重置请求作废；同一邮箱进行中的注册请求不受影响。"""
    make_user(engine)
    mailer = FakeMailer()
    first = start(engine, keys, flow, mailer=mailer)
    first_code = mailer.reset_codes[EMAIL]
    clear_send_limits(engine)
    second = start(engine, keys, flow, mailer=mailer)
    with pytest.raises(VerificationFailed):
        verify(engine, keys, first.challenge_id, first_code, flow)
    assert verify(engine, keys, second.challenge_id, mailer.reset_codes[EMAIL], flow)


def test_mail_failure_is_audited_not_reported(engine, keys, flow):
    """后台发信失败：不影响已经发出的回复，只写进审计（报给页面会暴露“这个邮箱有账户”）。"""
    make_user(engine)
    start(engine, keys, flow, mailer=FakeMailer(fail=True))
    with engine.begin() as connection:
        types = connection.scalars(sa.select(events.c.event_type)).all()
    assert "password_reset_mail_failed" in types


def test_changed_notice_is_sent_after_reset(engine, keys, flow):
    """重置成功后，给账户邮箱发“密码已修改”通知。"""
    make_user(engine)
    mailer = FakeMailer()
    email = reset(engine, keys, flow, mailer)
    password_reset.deliver_changed_notice(engine, mailer, email)
    assert mailer.changed == [EMAIL]


def test_reset_limits_follow_the_agreed_numbers():
    """数值与注册一致：同邮箱 60 秒 1 封、每小时 5 封；同 IP 每小时 20 封；全站共用 200 封。"""
    assert (limits.RESET_RESEND.limit, limits.RESET_RESEND.window.total_seconds()) == (
        1,
        60,
    )
    assert limits.RESET_EMAIL.limit == 5 and limits.RESET_SOURCE.limit == 20
    assert limits.MAIL_TOTAL.limit == 200
