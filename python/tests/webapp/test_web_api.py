"""网页接口测试：用 FastAPI 的模拟浏览器（TestClient）连真实测试库，走一遍真实的 HTTP 请求。

模拟浏览器会像真浏览器一样保存和发送 Cookie。网址用 https://testserver，
因为带 Secure 属性的 Cookie 只会通过 https 发送。
"""

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from webapp.api.cookies import COOKIE_NAME
from webapp.app import create_app
from webapp.core.app_config import AppConfig
from webapp.db.tables import sessions
from webapp.services.sessions import lookup

ORIGIN = "https://testserver"
EMAIL = "web.user@example.com"
PASSWORD = "river tea lamp"
NEW_PASSWORD = "seven blue kites"


class FakeMailer:
    def __init__(self):
        self.codes = {}
        self.notices = []
        self.reset_codes = {}
        self.changed = []

    def send_registration_code(self, to, code):
        self.codes[to] = code

    def send_already_registered(self, to):
        self.notices.append(to)

    def send_password_reset_code(self, to, code):
        self.reset_codes[to] = code

    def send_password_changed(self, to):
        self.changed.append(to)


def dev_config(**changes) -> AppConfig:
    values = {
        "environment": "development",
        "allowed_origins": (ORIGIN,),
        "allowed_hosts": ("testserver",),
        "mail_mode": "console",
    }
    return AppConfig(**{**values, **changes})


@pytest.fixture
def mailer():
    return FakeMailer()


@pytest.fixture
def app(engine, keys, mailer):
    return create_app(engine=engine, keys=keys, config=dev_config(), mailer=mailer)


@pytest.fixture
def browser(app):
    """一个“浏览器”：自带 Cookie 保存，默认带上我们自己网站的 Origin。"""
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as client:
        yield client


def csrf(browser) -> str:
    response = browser.get("/api/auth/csrf")
    assert response.status_code == 200
    return response.json()["csrf_token"]


def post(browser, path, body=None, token=None):
    headers = {"X-CSRF-Token": token} if token else {}
    return browser.post(path, json=body or {}, headers=headers)


def register(browser, mailer, email=EMAIL, password=PASSWORD):
    token = csrf(browser)
    started = post(browser, "/api/auth/registration/request", {"email": email}, token)
    assert started.status_code == 202, started.text
    verified = post(
        browser,
        "/api/auth/registration/verify",
        {"challenge_id": started.json()["challenge_id"], "code": mailer.codes[email]},
        token,
    )
    assert verified.status_code == 200, verified.text
    done = post(
        browser,
        "/api/auth/registration/complete",
        {
            "registration_token": verified.json()["registration_token"],
            "password": password,
        },
        token,
    )
    assert done.status_code == 201, done.text


def login(browser, email=EMAIL, password=PASSWORD):
    response = post(
        browser,
        "/api/auth/login",
        {"email": email, "password": password},
        csrf(browser),
    )
    return response


def test_full_flow_register_login_me_logout(browser, mailer):
    """完整走一遍：注册三步 → 登录 → 查看自己的账户 → 退出 → 再查看就要求登录。"""
    register(browser, mailer)
    logged_in = login(browser)
    assert logged_in.status_code == 200
    assert logged_in.json()["account"]["email"] == EMAIL
    me = browser.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["email"] == EMAIL
    assert browser.get("/api/auth/csrf").json()["authenticated"] is True
    out = post(browser, "/api/auth/logout", token=logged_in.json()["csrf_token"])
    assert out.status_code == 200
    assert browser.get("/api/auth/me").status_code == 401


def test_session_cookie_has_all_security_attributes(browser):
    """Cookie 带齐 HttpOnly、Secure、SameSite=Lax、Path=/，且不设置 Domain（验收 A03）；
    接口响应禁止缓存，并带安全响应头。"""
    response = browser.get("/api/auth/csrf")
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    lowered = cookie.lower()
    for attribute in ("httponly", "secure", "samesite=lax", "path=/"):
        assert attribute in lowered
    assert "domain=" not in lowered
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-request-id"]


def test_csrf_token_survives_a_page_refresh(browser):
    """刷新页面后再取一次，得到的是同一个 CSRF 令牌，而且不会新建会话。"""
    first = csrf(browser)
    cookie = browser.cookies.get(COOKIE_NAME)
    assert csrf(browser) == first
    assert browser.cookies.get(COOKIE_NAME) == cookie


@pytest.mark.parametrize(
    "headers",
    [
        {},  # 没有 CSRF 令牌
        {"X-CSRF-Token": "wrong-token"},  # 令牌不对
        {"Origin": "https://evil.example"},  # 来自别的网站
    ],
)
def test_state_changes_need_origin_and_csrf(browser, headers):
    """修改操作缺少令牌、令牌错误、来源是别的网站，都返回 403（验收 A04）。"""
    token = csrf(browser)
    sent = {"X-CSRF-Token": token, **headers} if "Origin" in headers else headers
    response = browser.post(
        "/api/auth/registration/request", json={"email": EMAIL}, headers=sent
    )
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"


def test_missing_origin_and_referer_is_refused_but_referer_alone_works(app):
    """既没有 Origin 也没有 Referer：拒绝，不默认放行；只有同站 Referer 时可以通过。"""
    with TestClient(app, base_url=ORIGIN) as client:
        token = client.get("/api/auth/csrf").json()["csrf_token"]
        refused = client.post(
            "/api/auth/registration/request",
            json={"email": EMAIL},
            headers={"X-CSRF-Token": token},
        )
        assert refused.status_code == 403
        allowed = client.post(
            "/api/auth/registration/request",
            json={"email": EMAIL},
            headers={"X-CSRF-Token": token, "Referer": f"{ORIGIN}/register"},
        )
        assert allowed.status_code == 202


def test_login_replaces_the_anonymous_session(browser, mailer, engine, keys):
    """登录后换成新令牌，原来的匿名令牌被撤销，不能当登录凭证用（防会话固定，验收 A03）。"""
    register(browser, mailer)
    anonymous = browser.cookies.get(COOKIE_NAME)
    assert login(browser).status_code == 200
    user_token = browser.cookies.get(COOKIE_NAME)
    assert user_token != anonymous
    assert lookup(engine, keys, anonymous) is None
    assert lookup(engine, keys, user_token).kind == "user"


def test_copied_cookie_stops_working_after_logout(app, browser, mailer):
    """退出后，事先复制走的 Cookie 也立即无效（服务端撤销，不只是删浏览器 Cookie，验收 A05）。"""
    register(browser, mailer)
    token = login(browser).json()["csrf_token"]
    copied = browser.cookies.get(COOKIE_NAME)
    post(browser, "/api/auth/logout", token=token)
    with TestClient(app, base_url=ORIGIN) as thief:
        thief.cookies.set(COOKIE_NAME, copied)
        assert thief.get("/api/auth/me").status_code == 401


def test_login_failures_are_uniform_and_rate_limited(browser, mailer):
    """邮箱不存在和密码错误返回同样的 401；同一来源连续失败 5 次后返回 429 和 Retry-After。"""
    register(browser, mailer)
    unknown = login(browser, email="nobody@example.com")
    wrong = login(browser, password="wrong password")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["message"] == wrong.json()["message"] == "账号或密码错误"
    for _ in range(4):
        login(browser, password="wrong password")
    limited = login(browser)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0


def test_change_password_logs_out_everywhere(app, browser, mailer):
    """改密码后：当前和其他设备都要重新登录，旧密码不能用，新密码可以登录（验收 A15）。"""
    register(browser, mailer)
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as other:
        assert login(other).status_code == 200
        token = login(browser).json()["csrf_token"]
        changed = post(
            browser,
            "/api/auth/password/change",
            {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            token,
        )
        assert changed.status_code == 200
        assert browser.get("/api/auth/me").status_code == 401
        assert other.get("/api/auth/me").status_code == 401
    assert login(browser).status_code == 401
    assert login(browser, password=NEW_PASSWORD).status_code == 200


def test_logout_all_and_delete_account_need_the_current_password(
    browser, mailer, engine
):
    """退出所有设备、注销账户都要求当前密码；注销后无法登录，会话全部消失（验收 A19）。"""
    register(browser, mailer)
    token = login(browser).json()["csrf_token"]
    wrong = post(
        browser, "/api/auth/logout-all", {"current_password": "nope nope"}, token
    )
    assert wrong.status_code == 401
    deleted = post(
        browser, "/api/auth/account/delete", {"current_password": PASSWORD}, token
    )
    assert deleted.status_code == 200
    assert browser.get("/api/auth/me").status_code == 401
    assert login(browser).status_code == 401
    with engine.begin() as connection:
        kinds = connection.scalars(
            sa.select(sessions.c.kind).where(sessions.c.revoked_at.is_(None))
        ).all()
    assert "user" not in kinds


def test_user_endpoints_require_login(browser):
    """没有登录时，需要登录的接口返回 401。"""
    token = csrf(browser)
    assert browser.get("/api/auth/me").status_code == 401
    response = post(
        browser, "/api/auth/logout-all", {"current_password": PASSWORD}, token
    )
    assert response.status_code == 401


def test_invalid_input_never_echoes_back(browser):
    """格式不对的请求返回 400，只说哪个字段有问题，不回显输入内容（输入可能就是密码）。"""
    token = csrf(browser)
    secret = "my-secret-password-" + "x" * 2000
    response = post(
        browser, "/api/auth/login", {"email": EMAIL, "password": secret}, token
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert secret not in response.text and "my-secret" not in response.text
    extra = post(
        browser,
        "/api/auth/login",
        {"email": EMAIL, "password": "x", "admin": True},
        token,
    )
    assert extra.status_code == 400


def test_password_rules_are_reported_with_stable_codes(browser, mailer):
    """密码不符合规则时返回固定编号，前端据此显示提示。"""
    token = csrf(browser)
    started = post(browser, "/api/auth/registration/request", {"email": EMAIL}, token)
    verified = post(
        browser,
        "/api/auth/registration/verify",
        {"challenge_id": started.json()["challenge_id"], "code": mailer.codes[EMAIL]},
        token,
    )
    short = post(
        browser,
        "/api/auth/registration/complete",
        {
            "registration_token": verified.json()["registration_token"],
            "password": "short",
        },
        token,
    )
    assert short.status_code == 400 and short.json()["code"] == "password_too_short"


def test_secrets_never_reach_the_logs(browser, mailer, caplog):
    """整个流程中，日志里没有密码、验证码、令牌（验收 A12）。"""
    with caplog.at_level("DEBUG"):
        register(browser, mailer)
        response = login(browser)
    secrets = [PASSWORD, mailer.codes[EMAIL], browser.cookies.get(COOKIE_NAME)]
    secrets.append(response.json()["csrf_token"])
    for secret in secrets:
        assert secret not in caplog.text


def test_unknown_host_is_refused(app):
    """Host 请求头不在允许列表里的请求被拒绝（防伪造主机名）。"""
    with TestClient(app, base_url="https://evil.example") as client:
        assert client.get("/api/auth/csrf").status_code == 400


def test_docs_only_in_development(engine, keys, mailer):
    """接口说明页 /docs 只在开发环境提供。"""
    dev = create_app(engine=engine, keys=keys, config=dev_config(), mailer=mailer)
    prod = create_app(
        engine=engine,
        keys=keys,
        config=dev_config(environment="production", mail_mode="console"),
        mailer=mailer,
    )
    with TestClient(dev, base_url=ORIGIN) as client:
        assert client.get("/docs").status_code == 200
    with TestClient(prod, base_url=ORIGIN) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_database_outage_returns_503_not_success(keys, mailer, test_db):
    """账户库连不上时返回 503“暂时不可用”，不会放行或假装成功（验收 A10）。"""
    profile = test_db[0]
    broken = sa.create_engine(
        profile.url("runtime").set(port=1),
        connect_args={**profile.connect_args(), "connect_timeout": 2},
    )
    try:
        app = create_app(engine=broken, keys=keys, config=dev_config(), mailer=mailer)
        with TestClient(app, base_url=ORIGIN) as client:
            response = client.get("/api/auth/csrf")
        assert response.status_code == 503
        assert response.json()["code"] == "service_unavailable"
    finally:
        broken.dispose()


def test_password_reset_through_the_api(browser, mailer):
    """通过接口重置密码：有账户和没账户的回复除编号外完全一样；
    重置后旧登录失效、旧密码不能用、新密码能登录，并发出“密码已修改”通知（验收 A14）。"""
    register(browser, mailer)
    assert login(browser).status_code == 200
    token = csrf(browser)
    unknown = post(
        browser,
        "/api/auth/password-reset/request",
        {"email": "nobody@example.com"},
        token,
    )
    known = post(browser, "/api/auth/password-reset/request", {"email": EMAIL}, token)
    assert unknown.status_code == known.status_code == 202
    assert unknown.json()["message"] == known.json()["message"]
    assert set(unknown.json()) == set(known.json())
    assert list(mailer.reset_codes) == [EMAIL]  # 后台任务只给有账户的邮箱发了信

    verified = post(
        browser,
        "/api/auth/password-reset/verify",
        {
            "challenge_id": known.json()["challenge_id"],
            "code": mailer.reset_codes[EMAIL],
        },
        token,
    )
    assert verified.status_code == 200
    done = post(
        browser,
        "/api/auth/password-reset/complete",
        {"reset_token": verified.json()["reset_token"], "new_password": NEW_PASSWORD},
        token,
    )
    assert done.status_code == 200
    assert mailer.changed == [EMAIL]
    assert browser.get("/api/auth/me").status_code == 401  # 这台设备的旧登录也已失效
    assert login(browser).status_code == 401
    assert login(browser, password=NEW_PASSWORD).status_code == 200


def test_password_reset_with_a_bad_token_is_refused(browser):
    """重置凭证无效时返回 400 password_reset_failed。"""
    token = csrf(browser)
    response = post(
        browser,
        "/api/auth/password-reset/complete",
        {"reset_token": "made-up-token", "new_password": NEW_PASSWORD},
        token,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "password_reset_failed"
