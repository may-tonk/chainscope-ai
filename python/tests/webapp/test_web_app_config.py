"""运行配置、发信器和来源 IP 的测试（不需要数据库）。"""

import io
import ipaddress
from types import SimpleNamespace

import pytest

from webapp.api.dependencies import client_source
from webapp.core.app_config import load_app_config
from webapp.core.errors import ConfigError, CsrfRejected
from webapp.core.mailer import ConsoleMailer
from webapp.security.csrf import check_origin, request_origin


def test_development_defaults():
    """开发环境不填任何变量时：只允许 localhost:5173 来源，用控制台发信，开启 /docs。"""
    config = load_app_config({})
    assert config.environment == "development"
    assert config.allowed_origins == ("http://localhost:5173",)
    assert config.mail_mode == "console" and config.docs_enabled


@pytest.mark.parametrize(
    "values, message",
    [
        ({"WEBAPP_ENV": "staging"}, "WEBAPP_ENV"),
        ({"WEBAPP_ENV": "production"}, "必须配置"),
        (
            {
                "WEBAPP_ENV": "production",
                "WEBAPP_ALLOWED_ORIGINS": "http://site.example",
                "WEBAPP_ALLOWED_HOSTS": "site.example",
            },
            "https",
        ),
        (
            {
                "WEBAPP_ENV": "production",
                "WEBAPP_ALLOWED_ORIGINS": "https://site.example",
                "WEBAPP_ALLOWED_HOSTS": "site.example",
                "WEBAPP_MAIL_MODE": "console",
            },
            "console",
        ),
        ({"WEBAPP_ALLOWED_ORIGINS": "https://site.example/path"}, "格式不正确"),
        ({"WEBAPP_TRUSTED_PROXIES": "not-an-ip"}, "WEBAPP_TRUSTED_PROXIES"),
    ],
)
def test_unsafe_configuration_refuses_to_start(values, message):
    """生产环境缺少来源或主机、来源不是 https、使用控制台发信（验收 A21）等，都拒绝启动。"""
    with pytest.raises(ConfigError, match=message):
        load_app_config(values)


def test_console_mailer_prints_to_the_backend_window():
    """控制台发信把验证码打印在后端窗口。"""
    stream = io.StringIO()
    ConsoleMailer(stream).send_registration_code("a@example.com", "123456")
    assert "a@example.com" in stream.getvalue() and "123456" in stream.getvalue()


def test_origin_is_taken_from_origin_or_referer():
    """来源优先取 Origin，没有再从 Referer 推出；都没有就无法判断，检查不通过。"""
    assert request_origin({"origin": "https://a.example"}) == "https://a.example"
    assert request_origin({"referer": "https://a.example/x?y=1"}) == "https://a.example"
    assert request_origin({"origin": "null"}) is None
    assert request_origin({}) is None
    check_origin({"origin": "https://a.example"}, ("https://a.example",))
    with pytest.raises(CsrfRejected):
        check_origin({}, ("https://a.example",))


def fake_request(peer, forwarded=None, proxies=()):
    """伪造一个最小的请求对象：直接连接的地址、X-Forwarded-For 头、受信任代理配置。"""
    config = SimpleNamespace(
        trusted_proxies=tuple(ipaddress.ip_network(p) for p in proxies)
    )
    headers = {"x-forwarded-for": forwarded} if forwarded else {}
    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers=headers,
        app=SimpleNamespace(
            state=SimpleNamespace(webapp=SimpleNamespace(config=config))
        ),
    )


def test_forwarded_header_is_ignored_without_a_trusted_proxy():
    """没有配置受信任代理时，伪造的 X-Forwarded-For 不起作用（验收 A02）。"""
    assert client_source(fake_request("203.0.113.5", "1.2.3.4")) == "203.0.113.5"


def test_forwarded_header_is_read_through_a_trusted_proxy():
    """经过受信任代理时，从右往左取第一个不是代理的地址；用户自己伪造的最左边地址不会被采用。"""
    request = fake_request(
        "10.0.0.2", "6.6.6.6, 198.51.100.9, 10.0.0.1", proxies=("10.0.0.0/8",)
    )
    assert client_source(request) == "198.51.100.9"
    garbage = fake_request("10.0.0.2", "not-an-ip", proxies=("10.0.0.0/8",))
    assert client_source(garbage) == "10.0.0.2"
