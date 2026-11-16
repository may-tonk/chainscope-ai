"""共享限速（方案 6.2 节，按 13.4 节修正过的防恶意锁号规则）。

计数存在 PostgreSQL 的 auth_rate_limits 表里，而不是 Python 内存里：
以后开多个后端进程时，大家看到的是同一份计数，换一个进程不能绕过限制。

每条规则 = “在一个时间窗口内最多允许几次，达到上限后冷却多久”。用法分三步：
1. ensure_allowed()：做事之前先看是否在冷却中，是就直接拒绝（抛出 RateLimited）；
2. count()：记一次（登录失败、发送一封验证邮件等），达到上限时开始冷却；
3. clear()：需要时清零，例如登录成功后清掉“这个来源对这个邮箱”的失败计数。

被计数的对象（邮箱、IP 等）不直接存进数据库，而是先用 AUTH_RATE_LIMIT_KEY 算成 HMAC，
数据库里看不出是谁。时间统一用数据库的 now()，多个进程之间不会因为各自时钟不同而算错。

已知的小误差：两个请求恰好同时通过第 1 步检查，可能比上限多放过一两次；
计数本身是原子更新，不会丢失，下一次请求就会被拦住。
"""

import math
from dataclasses import dataclass
from datetime import timedelta

import sqlalchemy as sa

from ..core.errors import RateLimited
from ..core.keys import AuthKeys
from ..db.tables import rate_limits
from ..security.tokens import fingerprint


@dataclass(frozen=True)
class Rule:
    purpose: str  # 规则名，也是数据库里的 purpose 列
    limit: int  # 窗口内允许的次数，达到这个数就开始冷却
    window: timedelta  # 计数窗口长度，窗口过了计数从 1 重新开始
    block: timedelta | None = None  # 冷却多久；None 表示冷却到当前窗口结束
    # 逐步延迟：达到上限后不一次锁很久，而是每多一次，等待时间翻倍（最长 MAX_DELAY）。
    progressive: bool = False


MAX_DELAY = timedelta(minutes=5)

# ── 登录 ──
# 同一来源对同一邮箱：失败 5 次，这个来源冷却 15 分钟；其他来源不受影响（防恶意锁号）。
LOGIN_EMAIL_SOURCE = Rule(
    "login_email_source", 5, timedelta(minutes=15), timedelta(minutes=15)
)
# 同一邮箱从所有来源合计：每小时失败超过 100 次后逐步延迟（防分布式猜密码）。
LOGIN_EMAIL = Rule("login_email", 100, timedelta(hours=1), progressive=True)
# 同一来源（IP）：每分钟最多 20 次登录尝试，成功失败都算。
LOGIN_SOURCE = Rule("login_source", 20, timedelta(minutes=1))

# ── 注册验证邮件 ──
REGISTER_EMAIL = Rule(
    "register_email", 5, timedelta(hours=1)
)  # 同邮箱每小时最多发 5 封
REGISTER_RESEND = Rule(
    "register_resend", 1, timedelta(seconds=60)
)  # 同邮箱 60 秒内只发 1 封
REGISTER_SOURCE = Rule(
    "register_source", 20, timedelta(hours=1)
)  # 同 IP 每小时最多 20 封
# 整个网站每小时最多发 200 封验证邮件，注册和重置密码共用这个总预算。
MAIL_TOTAL = Rule("mail_total", 200, timedelta(hours=1))
# ── 重置密码验证邮件（2c，数值与注册一致）──
RESET_EMAIL = Rule("reset_email", 5, timedelta(hours=1))  # 同邮箱每小时最多 5 封
RESET_RESEND = Rule("reset_resend", 1, timedelta(seconds=60))  # 同邮箱 60 秒内只发 1 封
RESET_SOURCE = Rule("reset_source", 20, timedelta(hours=1))  # 同 IP 每小时最多 20 封

# 同邮箱输错验证码（注册和重置合计）：每小时累计 10 次就冷却。
# 重新获取验证码不会清零，防止“换个请求接着猜”。
VERIFY_EMAIL = Rule("verify_email", 10, timedelta(hours=1))

# ── 匿名会话（打开注册、登录页面时创建）：同一来源每小时最多新建 60 个 ──
ANONYMOUS_SESSION = Rule("anonymous_session", 60, timedelta(hours=1))

# ── 已登录后需要输入当前密码的操作（改密码、退出所有设备、注销）──
ACCOUNT_PASSWORD = Rule(
    "account_password", 5, timedelta(minutes=15), timedelta(minutes=15)
)


def _key(keys: AuthKeys, rule: Rule, parts) -> bytes:
    """把被计数的对象（如 邮箱、IP）变成匿名的 HMAC 键。"""
    return fingerprint(keys.rate_limit, rule.purpose, *[str(p) for p in parts])


def retry_after(connection, keys: AuthKeys, rule: Rule, *parts) -> int | None:
    """如果正在冷却，返回还要等几秒（向上取整）；没在冷却返回 None。"""
    seconds = connection.scalar(
        sa.select(
            sa.extract("epoch", rate_limits.c.blocked_until - sa.func.now())
        ).where(
            rate_limits.c.purpose == rule.purpose,
            rate_limits.c.subject_key == _key(keys, rule, parts),
            rate_limits.c.blocked_until > sa.func.now(),
        )
    )
    return None if seconds is None else max(1, math.ceil(float(seconds)))


def ensure_allowed(connection, keys: AuthKeys, *checks) -> None:
    """检查多条规则，任何一条在冷却中就抛出 RateLimited（等待时间取最长的那条）。

    checks 的每一项是 (规则, 被计数对象…)，例如 (LOGIN_SOURCE, "203.0.113.5")。
    """
    waits = [retry_after(connection, keys, rule, *parts) for rule, *parts in checks]
    waits = [w for w in waits if w is not None]
    if waits:
        raise RateLimited(max(waits))


# 原子地“记一次”：没有记录就新建；有记录时，窗口已过就从 1 重新计，否则加 1。
# ON CONFLICT 让并发请求排队更新同一行，计数不会丢失。
# UPDATE 里的表达式读到的都是更新前的旧值，所以 attempts 和 window_start 的先后顺序无关。
COUNT = sa.text(
    """
    INSERT INTO auth_rate_limits AS r
        (purpose, subject_key, window_start, attempts, updated_at)
    VALUES (:purpose, :key, now(), 1, now())
    ON CONFLICT (purpose, subject_key) DO UPDATE SET
        attempts = CASE WHEN r.window_start <= now() - :window
                        THEN 1 ELSE r.attempts + 1 END,
        window_start = CASE WHEN r.window_start <= now() - :window
                            THEN now() ELSE r.window_start END,
        updated_at = now()
    RETURNING attempts, window_start
    """
)


def count(connection, keys: AuthKeys, rule: Rule, *parts) -> int:
    """记一次，返回当前窗口内的次数；达到上限时写入冷却结束时间。"""
    key = _key(keys, rule, parts)
    attempts, window_start = connection.execute(
        COUNT, {"purpose": rule.purpose, "key": key, "window": rule.window}
    ).one()
    if attempts >= rule.limit:
        if rule.progressive:
            # 第 limit 次等 1 秒，之后每多一次翻倍：1、2、4、8……最多 5 分钟。
            # 指数先封顶到 10（1024 秒），避免次数很大时算出天文数字导致溢出。
            exponent = min(attempts - rule.limit, 10)
            delay = min(timedelta(seconds=2**exponent), MAX_DELAY)
            until = sa.func.now() + delay
        elif rule.block is not None:
            until = sa.func.now() + rule.block
        else:
            until = sa.literal(window_start + rule.window)
        connection.execute(
            rate_limits.update()
            .where(
                rate_limits.c.purpose == rule.purpose,
                rate_limits.c.subject_key == key,
            )
            .values(blocked_until=until)
        )
    return attempts


def clear(connection, keys: AuthKeys, rule: Rule, *parts) -> None:
    """清零某条规则对某个对象的计数（例如登录成功后）。"""
    connection.execute(
        rate_limits.delete().where(
            rate_limits.c.purpose == rule.purpose,
            rate_limits.c.subject_key == _key(keys, rule, parts),
        )
    )
