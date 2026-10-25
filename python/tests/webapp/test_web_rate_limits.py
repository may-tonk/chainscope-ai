"""共享限速测试（真实测试库）。"""

from datetime import timedelta

import pytest
import sqlalchemy as sa

from webapp.core.errors import RateLimited
from webapp.db.tables import rate_limits
from webapp.services import rate_limits as limits

EMAIL = "victim@example.com"
ATTACKER = "198.51.100.7"
OWNER = "203.0.113.5"


def fail(engine, keys, rule, *parts, times=1):
    for _ in range(times):
        with engine.begin() as connection:
            limits.count(connection, keys, rule, *parts)


def blocked(engine, keys, *checks) -> int | None:
    with engine.begin() as connection:
        try:
            limits.ensure_allowed(connection, keys, *checks)
        except RateLimited as exc:
            return exc.retry_after
    return None


def test_cooldown_starts_at_the_limit_and_only_for_that_source(engine, keys):
    """同一来源对同一邮箱失败 4 次仍可尝试，第 5 次后冷却约 15 分钟；
    换一个来源（真实用户）不受影响——攻击者没法把别人锁住（验收 A20）。"""
    rule = limits.LOGIN_EMAIL_SOURCE
    fail(engine, keys, rule, EMAIL, ATTACKER, times=4)
    assert blocked(engine, keys, (rule, EMAIL, ATTACKER)) is None
    fail(engine, keys, rule, EMAIL, ATTACKER)
    wait = blocked(engine, keys, (rule, EMAIL, ATTACKER))
    assert 14 * 60 < wait <= 15 * 60
    assert blocked(engine, keys, (rule, EMAIL, OWNER)) is None


def test_counts_restart_after_the_window_and_clear_removes_them(engine, keys):
    """窗口过去后重新从 1 计数；clear() 直接清零。"""
    rule = limits.LOGIN_EMAIL_SOURCE
    fail(engine, keys, rule, EMAIL, ATTACKER, times=3)
    with engine.begin() as connection:
        connection.execute(
            rate_limits.update().values(
                window_start=sa.func.now() - rule.window - timedelta(seconds=1)
            )
        )
        assert limits.count(connection, keys, rule, EMAIL, ATTACKER) == 1
        limits.clear(connection, keys, rule, EMAIL, ATTACKER)
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(rate_limits)) == 0
        )


def test_window_rule_blocks_until_the_window_ends(engine, keys):
    """没有单独冷却时长的规则（如每 60 秒 1 封邮件）：达到上限后冷却到窗口结束。"""
    rule = limits.REGISTER_RESEND
    fail(engine, keys, rule, EMAIL)
    wait = blocked(engine, keys, (rule, EMAIL))
    assert 55 <= wait <= 60


def test_email_level_limit_delays_progressively(engine, keys):
    """邮箱级总上限：达到 100 次后先等 1 秒，之后每多一次等待翻倍，而不是一次锁死。"""
    rule = limits.LOGIN_EMAIL
    with engine.begin() as connection:
        connection.execute(
            rate_limits.insert().values(
                purpose=rule.purpose,
                subject_key=limits._key(keys, rule, (EMAIL,)),
                window_start=sa.func.now(),
                attempts=rule.limit - 1,
            )
        )
        assert limits.count(connection, keys, rule, EMAIL) == rule.limit
    assert blocked(engine, keys, (rule, EMAIL)) == 1
    fail(engine, keys, rule, EMAIL, times=3)  # 第 103 次 → 2^3 = 8 秒
    assert 7 <= blocked(engine, keys, (rule, EMAIL)) <= 8
    with engine.begin() as connection:
        connection.execute(rate_limits.update().values(attempts=10_000))
        limits.count(connection, keys, rule, EMAIL)
    assert blocked(engine, keys, (rule, EMAIL)) <= limits.MAX_DELAY.total_seconds()


def test_counters_store_only_hmac_keys(engine, keys):
    """限速表里存的是 HMAC，看不到邮箱和 IP 原文。"""
    fail(engine, keys, limits.LOGIN_EMAIL_SOURCE, EMAIL, ATTACKER)
    with engine.begin() as connection:
        stored = connection.scalar(sa.select(rate_limits.c.subject_key))
    assert len(stored) == 32
    assert EMAIL.encode() not in stored and ATTACKER.encode() not in stored


def test_several_rules_report_the_longest_wait(engine, keys):
    """同时检查多条规则时，按等待时间最长的那条报。"""
    fail(engine, keys, limits.REGISTER_RESEND, EMAIL)
    fail(engine, keys, limits.LOGIN_EMAIL_SOURCE, EMAIL, ATTACKER, times=5)
    wait = blocked(
        engine,
        keys,
        (limits.REGISTER_RESEND, EMAIL),
        (limits.LOGIN_EMAIL_SOURCE, EMAIL, ATTACKER),
    )
    assert wait > 60


def test_rate_limited_error_never_reports_zero_seconds():
    """Retry-After 至少 1 秒。"""
    assert RateLimited(0).retry_after == 1


@pytest.mark.parametrize("rule", [limits.LOGIN_SOURCE, limits.MAIL_TOTAL])
def test_attempt_rules_allow_exactly_the_limit(engine, keys, rule):
    """“每窗口最多 N 次”的规则：前 N 次都放行，第 N 次之后才拦截。"""
    for _ in range(rule.limit):
        assert blocked(engine, keys, (rule, OWNER)) is None
        fail(engine, keys, rule, OWNER)
    assert blocked(engine, keys, (rule, OWNER)) is not None
