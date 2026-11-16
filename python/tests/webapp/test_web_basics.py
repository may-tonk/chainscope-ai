"""不需要数据库的基础部件测试：密钥读取、令牌与指纹、邮箱格式、来源脱敏、哈希并发上限。"""

import base64
import secrets
import subprocess
import sys
import threading

import pytest

from webapp.core.emails import normalize_email
from webapp.core.errors import ConfigError, InvalidEmail, ServiceBusy
from webapp.core.keys import KEY_NAMES, load_keys
from webapp.core.settings import ROOT
from webapp.security import passwords
from webapp.security.tokens import fingerprint, new_code, new_token, same
from webapp.services.audit import mask_source


def key_text(n=32) -> str:
    """生成一段 base64url 文本形式的随机密钥（默认 32 字节）。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(n)).decode().rstrip("=")


@pytest.fixture
def key_values():
    """一组合格的 4 把密钥配置。"""
    return {name: key_text() for name in KEY_NAMES}


# ── 密钥 ──


def test_keys_load_and_are_hidden_when_printed(key_values):
    """4 把密钥正确读取为 32 字节；打印对象时看不到密钥内容。"""
    keys = load_keys(key_values)
    assert len(keys.session) == 32 and keys.version == 1
    assert repr(keys) == "AuthKeys(version=1)"


@pytest.mark.parametrize(
    "change, message",
    [
        ({"AUTH_CSRF_KEY": ""}, "AUTH_CSRF_KEY"),
        ({"AUTH_CSRF_KEY": key_text(16)}, "太短"),
        ({"AUTH_CSRF_KEY": "not base64!"}, "格式不正确"),
        ({"AUTH_KEY_VERSION": "0"}, "AUTH_KEY_VERSION"),
        ({"AUTH_KEY_VERSION": "abc"}, "AUTH_KEY_VERSION"),
    ],
)
def test_bad_keys_refuse_to_start(key_values, change, message):
    """缺少、太短、格式不对、版本号不对，都拒绝启动，并指出是哪个变量。"""
    with pytest.raises(ConfigError, match=message):
        load_keys({**key_values, **change})


def test_keys_must_differ(key_values):
    """4 把密钥不能有相同的。"""
    same_key = key_values["AUTH_SESSION_KEY"]
    with pytest.raises(ConfigError, match="互不相同"):
        load_keys({**key_values, "AUTH_CSRF_KEY": same_key})


def test_key_generator_output_loads():
    """生成工具打印的 4 行可以直接被 load_keys 读取。"""
    output = subprocess.run(
        [sys.executable, str(ROOT / "python" / "tools" / "generate_webapp_keys.py")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    values = dict(line.split("=", 1) for line in output.split())
    assert set(values) == set(KEY_NAMES)
    assert load_keys(values).version == 1


# ── 令牌与指纹 ──


def test_tokens_and_codes_are_random_and_well_formed():
    """令牌约 43 个字符且不重复；验证码固定 6 位数字（保留开头的 0）。"""
    tokens = {new_token() for _ in range(100)}
    assert len(tokens) == 100 and all(len(t) == 43 for t in tokens)
    codes = [new_code() for _ in range(200)]
    assert all(len(c) == 6 and c.isdigit() for c in codes)


def test_fingerprint_depends_on_key_purpose_and_parts():
    """密钥、用途、内容任一不同，指纹都不同；分隔符防止拼接歧义。"""
    key = secrets.token_bytes(32)
    base = fingerprint(key, "session", "abc")
    assert len(base) == 32
    assert fingerprint(key, "session", "abc") == base
    assert fingerprint(key, "csrf", "abc") != base
    assert fingerprint(secrets.token_bytes(32), "session", "abc") != base
    assert fingerprint(key, "x", "ab", "c") != fingerprint(key, "x", "a", "bc")
    assert same(base, fingerprint(key, "session", "abc"))
    assert not same(base, fingerprint(key, "session", "abd"))


# ── 邮箱格式 ──


@pytest.mark.parametrize(
    "raw, expected",
    [
        (
            "  Alice.Smith+news@Example.COM ",
            ("Alice.Smith+news@Example.COM", "alice.smith+news@example.com"),
        ),
        ("a@b.cn", ("a@b.cn", "a@b.cn")),
    ],
)
def test_valid_emails_keep_display_form_and_lowercase_key(raw, expected):
    """去掉首尾空白；显示地址保留原样，email_key 转小写；点和 + 后缀都保留。"""
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "no-at-sign",
        "two@@example.com",
        "a@b@example.com",
        ".dot@example.com",
        "dot.@example.com",
        "two..dots@example.com",
        "user@localhost",
        "user@example.c",
        "user@example.123",
        "user@-bad.com",
        "用户@example.com",
        "a" * 65 + "@example.com",
        "a@" + "b" * 250 + ".com",
    ],
)
def test_invalid_emails_are_rejected(raw):
    """格式不对的邮箱一律拒绝，只报格式问题。"""
    with pytest.raises(InvalidEmail):
        normalize_email(raw)


# ── 来源脱敏 ──


@pytest.mark.parametrize(
    "source, masked",
    [
        ("203.0.113.57", "203.0.113.x"),
        ("2001:db8:abcd:12::1", "2001:db8:abcd::/48"),
        ("not-an-ip", "other"),
        (None, None),
    ],
)
def test_sources_are_masked_before_auditing(source, masked):
    """审计只保存脱敏后的来源：IPv4 去掉最后一段，IPv6 只留前 48 位。"""
    assert mask_source(source) == masked


# ── 哈希并发上限 ──


def test_hashing_reports_busy_when_all_slots_are_taken(monkeypatch):
    """4 个名额都被占用、等待超时时，返回“服务繁忙”而不是无限排队；名额归还后恢复正常。"""
    monkeypatch.setattr(passwords, "HASH_WAIT_SECONDS", 0.05)
    held = [passwords._hash_slots.acquire() for _ in range(passwords.HASH_SLOTS)]
    try:
        assert all(held)
        with pytest.raises(ServiceBusy):
            passwords.hash_password("river tea lamp orbit 47")
    finally:
        for _ in held:
            passwords._hash_slots.release()
    assert passwords.hash_password("river tea lamp orbit 47").startswith("$argon2id$")


def test_concurrent_hashing_never_exceeds_the_slot_limit(monkeypatch):
    """很多线程同时算哈希时，同一时刻最多只有 4 个在算。"""
    active = 0
    peak = 0
    lock = threading.Lock()
    real = passwords.hasher.hash

    def tracked(value):
        """包装真正的哈希函数：统计同一时刻有多少个哈希在算。"""
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            return real(value)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(passwords.hasher, "hash", tracked)
    threads = [
        threading.Thread(
            target=passwords.hash_password, args=("river tea lamp orbit 47",)
        )
        for _ in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert 1 <= peak <= passwords.HASH_SLOTS
