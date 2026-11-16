"""注册流程测试（真实测试库）。邮件用 FakeMailer 代替，验证码从它“收件箱”里取。"""

import threading
import uuid

import pytest
import sqlalchemy as sa

from webapp.core.errors import (
    InvalidEmail,
    MailDeliveryFailed,
    PasswordRejected,
    RateLimited,
    RegistrationFailed,
    VerificationFailed,
)
from webapp.db.tables import challenges, events, rate_limits, users
from webapp.security.passwords import verify_password
from webapp.services import registration
from webapp.services.sessions import start_anonymous

PASSWORD = "river tea lamp orbit 47"
SOURCE = "203.0.113.5"


class FakeMailer:
    """记录“发出”的邮件；fail=True 时模拟发送失败。"""

    def __init__(self, fail=False):
        self.codes = {}
        self.notices = []
        self.fail = fail

    def send_registration_code(self, to, code):
        """假发信：记下注册验证码；fail=True 时模拟发送失败。"""
        if self.fail:
            raise MailDeliveryFailed()
        self.codes[to] = code

    def send_already_registered(self, to):
        """假发信：记下收到“已有账户”提醒的邮箱。"""
        if self.fail:
            raise MailDeliveryFailed()
        self.notices.append(to)


@pytest.fixture
def flow(engine, keys):
    """一个浏览器匿名会话，代表“正在注册的那个浏览器”。"""
    return start_anonymous(engine, keys).session_id


def start(engine, keys, mailer, email, flow, source=SOURCE):
    """第 1 步的简写，返回请求编号。"""
    return registration.start(
        engine, keys, mailer, email, flow_session_id=flow, source=source
    ).challenge_id


def verify(engine, keys, challenge_id, code, flow):
    """第 2 步的简写，返回注册凭证。"""
    return registration.verify(
        engine, keys, challenge_id, code, flow_session_id=flow, source=SOURCE
    )


def complete(engine, keys, token, flow, password=PASSWORD):
    """第 3 步的简写，返回新账户的 user_id。"""
    return registration.complete(
        engine, keys, token, password, flow_session_id=flow, source=SOURCE
    )


def wrong(code: str) -> str:
    """生成一个肯定不对的验证码（正确值加 1）。"""
    return f"{(int(code) + 1) % 1_000_000:06d}"


def test_full_registration_creates_one_account(engine, keys, flow):
    """三步走完：账户创建、密码是 Argon2id 哈希、数据库里看不到验证码和凭证原文。"""
    mailer = FakeMailer()
    challenge_id = start(engine, keys, mailer, " New.User@Example.com ", flow)
    code = mailer.codes["New.User@Example.com"]
    token = verify(engine, keys, challenge_id, code, flow)
    user_id = complete(engine, keys, token, flow)
    with engine.begin() as connection:
        user = connection.execute(sa.select(users)).one()
        challenge = connection.execute(sa.select(challenges)).one()
        types = connection.scalars(sa.select(events.c.event_type)).all()
    assert user.user_id == user_id
    assert (user.email, user.email_key) == (
        "New.User@Example.com",
        "new.user@example.com",
    )
    assert verify_password(PASSWORD, user.password_hash) == (True, None)
    assert challenge.consumed_at is not None
    assert code.encode() not in challenge.code_hmac
    assert token.encode() not in challenge.registration_token_hash
    assert types == ["register_requested", "register_verified", "register_completed"]


def test_existing_email_gets_a_notice_and_no_usable_code(engine, keys, flow):
    """已注册的邮箱：对外照样返回请求编号，但只收到“已有账户”提醒，任何验证码都通不过（防探测）。"""
    mailer = FakeMailer()
    first = start(engine, keys, mailer, "taken@example.com", flow)
    complete(
        engine,
        keys,
        verify(engine, keys, first, mailer.codes["taken@example.com"], flow),
        flow,
    )
    with engine.begin() as connection:
        connection.execute(rate_limits.delete())  # 跳过 60 秒重发间隔
    second = start(engine, keys, mailer, "TAKEN@example.com", flow)
    assert isinstance(second, uuid.UUID)
    assert mailer.notices == ["TAKEN@example.com"]
    with pytest.raises(VerificationFailed):
        verify(engine, keys, second, "000000", flow)


def test_wrong_codes_are_limited_per_request(engine, keys, flow):
    """输错 5 次后，这个请求作废，正确的验证码也不能用了；错误次数随失败一起保存。"""
    mailer = FakeMailer()
    challenge_id = start(engine, keys, mailer, "a@example.com", flow)
    code = mailer.codes["a@example.com"]
    for _ in range(registration.MAX_CODE_FAILURES):
        with pytest.raises(VerificationFailed):
            verify(engine, keys, challenge_id, wrong(code), flow)
    with pytest.raises(VerificationFailed):
        verify(engine, keys, challenge_id, code, flow)
    with engine.begin() as connection:
        assert connection.scalar(sa.select(challenges.c.failed_attempts)) == 5


def test_email_level_failures_survive_new_requests(engine, keys, flow):
    """同一邮箱累计输错 10 次后冷却：重新获取验证码不会清零（不能“换个请求接着猜”）。"""
    mailer = FakeMailer()
    for _ in range(2):
        with engine.begin() as connection:
            connection.execute(
                rate_limits.delete().where(rate_limits.c.purpose != "verify_email")
            )
        challenge_id = start(engine, keys, mailer, "b@example.com", flow)
        code = mailer.codes["b@example.com"]
        for _ in range(5):
            with pytest.raises(VerificationFailed):
                verify(engine, keys, challenge_id, wrong(code), flow)
    with engine.begin() as connection:
        connection.execute(
            rate_limits.delete().where(rate_limits.c.purpose != "verify_email")
        )
    challenge_id = start(engine, keys, mailer, "b@example.com", flow)
    with pytest.raises(RateLimited):
        verify(engine, keys, challenge_id, mailer.codes["b@example.com"], flow)


def test_resend_invalidates_the_previous_code(engine, keys, flow):
    """重新获取验证码后，旧请求作废；60 秒内不能连续重发。"""
    mailer = FakeMailer()
    old = start(engine, keys, mailer, "c@example.com", flow)
    old_code = mailer.codes["c@example.com"]
    with pytest.raises(RateLimited):
        start(engine, keys, mailer, "c@example.com", flow)
    with engine.begin() as connection:
        connection.execute(rate_limits.delete())
    new = start(engine, keys, mailer, "c@example.com", flow)
    with pytest.raises(VerificationFailed):
        verify(engine, keys, old, old_code, flow)
    assert verify(engine, keys, new, mailer.codes["c@example.com"], flow)


def test_code_is_bound_to_the_browser_flow_and_expires(engine, keys, flow):
    """别的浏览器拿到验证码也用不了；验证码过期后失效。"""
    mailer = FakeMailer()
    challenge_id = start(engine, keys, mailer, "d@example.com", flow)
    code = mailer.codes["d@example.com"]
    other_flow = start_anonymous(engine, keys).session_id
    with pytest.raises(VerificationFailed):
        verify(engine, keys, challenge_id, code, other_flow)
    with engine.begin() as connection:
        connection.execute(challenges.update().values(expires_at=sa.func.now()))
    with pytest.raises(VerificationFailed):
        verify(engine, keys, challenge_id, code, flow)


def test_code_and_token_can_only_be_used_once(engine, keys, flow):
    """验证码只能验证一次；注册凭证只能用一次；凭证过期或换了浏览器也不能用。"""
    mailer = FakeMailer()
    challenge_id = start(engine, keys, mailer, "e@example.com", flow)
    code = mailer.codes["e@example.com"]
    token = verify(engine, keys, challenge_id, code, flow)
    with pytest.raises(VerificationFailed):
        verify(engine, keys, challenge_id, code, flow)
    with pytest.raises(RegistrationFailed):
        complete(engine, keys, token, start_anonymous(engine, keys).session_id)
    complete(engine, keys, token, flow)
    with pytest.raises(RegistrationFailed):
        complete(engine, keys, token, flow)
    with pytest.raises(RegistrationFailed):
        complete(engine, keys, "x" * 500, flow)


def test_expired_registration_token_is_refused(engine, keys, flow):
    """注册凭证 10 分钟过期后不能再用。"""
    mailer = FakeMailer()
    challenge_id = start(engine, keys, mailer, "f@example.com", flow)
    token = verify(engine, keys, challenge_id, mailer.codes["f@example.com"], flow)
    with engine.begin() as connection:
        connection.execute(
            challenges.update().values(registration_expires_at=sa.func.now())
        )
    with pytest.raises(RegistrationFailed):
        complete(engine, keys, token, flow)


def test_weak_password_keeps_the_token_usable(engine, keys, flow):
    """密码不符合规则时报具体原因，凭证不会被用掉，换个密码可以继续完成注册。"""
    mailer = FakeMailer()
    challenge_id = start(engine, keys, mailer, "g@example.com", flow)
    token = verify(engine, keys, challenge_id, mailer.codes["g@example.com"], flow)
    with pytest.raises(PasswordRejected):
        complete(engine, keys, token, flow, password="short")
    assert complete(engine, keys, token, flow)


def test_concurrent_completion_creates_exactly_one_account(engine, keys, flow):
    """两个请求同时拿同一个凭证完成注册：只有一个成功，只产生一个账户（验收 A08）。"""
    mailer = FakeMailer()
    challenge_id = start(engine, keys, mailer, "h@example.com", flow)
    token = verify(engine, keys, challenge_id, mailer.codes["h@example.com"], flow)
    barrier = threading.Barrier(2)
    outcomes = []

    def run():
        """并发测试里每个线程执行的内容：等到两个线程同时就绪再提交。"""
        barrier.wait()
        try:
            outcomes.append(complete(engine, keys, token, flow))
        except RegistrationFailed:
            outcomes.append("failed")

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(map(str, outcomes)).count("failed") == 1
    with engine.begin() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(users)) == 1


def test_concurrent_verification_issues_one_token(engine, keys, flow):
    """同一个验证码同时提交两次：只有一次成功（验证码不能重放，验收 A07）。"""
    mailer = FakeMailer()
    challenge_id = start(engine, keys, mailer, "i@example.com", flow)
    code = mailer.codes["i@example.com"]
    barrier = threading.Barrier(2)
    outcomes = []

    def run():
        """并发测试里每个线程执行的内容：等到两个线程同时就绪再提交。"""
        barrier.wait()
        try:
            outcomes.append(verify(engine, keys, challenge_id, code, flow))
        except VerificationFailed:
            outcomes.append(None)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count(None) == 1


def test_mail_failure_is_reported_and_audited(engine, keys, flow):
    """邮件发不出去：明确报错，并留下审计记录，不会假装已经发送。"""
    with pytest.raises(MailDeliveryFailed):
        start(engine, keys, FakeMailer(fail=True), "j@example.com", flow)
    with engine.begin() as connection:
        types = connection.scalars(sa.select(events.c.event_type)).all()
    assert "register_mail_failed" in types


def test_invalid_email_is_rejected_before_anything_is_stored(engine, keys, flow):
    """邮箱格式不对：直接拒绝，不计数、不写库。"""
    with pytest.raises(InvalidEmail):
        start(engine, keys, FakeMailer(), "not-an-email", flow)
    with engine.begin() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(rate_limits)) == 0
        )
