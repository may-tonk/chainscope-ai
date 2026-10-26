"""注册流程（方案 4.2、4.3 节）：先验证邮箱，再设置密码。

三步，每一步都绑定发起注册的那个浏览器匿名会话（flow_session_id）：

1. start()：提交邮箱。生成 6 位验证码，数据库只存它的 HMAC，验证码通过邮件发给用户。
   不管这个邮箱有没有注册过，对外返回都一样：已注册的邮箱不生成验证码，
   只给邮箱主人发一封“你已经有账户了”的提醒，所以别人没法借注册页探测谁注册过。
2. verify()：提交验证码。核对通过后，签发一个 10 分钟有效、只能用一次的“注册凭证”。
3. complete()：凭注册凭证设置密码。在一个事务里用掉凭证并创建账户；
   两个请求同时拿同一个凭证来，只有一个能成功。注册成功后不自动登录，需要去登录页登录。

邮件怎么发由调用方传入的 mailer 决定（2b 实现：开发阶段打印在后端窗口）。
"""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..core.emails import normalize_email
from ..core.errors import (
    MailDeliveryFailed,
    RateLimited,
    RegistrationFailed,
    VerificationFailed,
)
from ..core.keys import AuthKeys
from ..core.mailer import Mailer
from ..db.tables import challenges, users
from ..security.passwords import check_new_password, hash_password
from ..security.tokens import CODE_DIGITS, fingerprint, new_code, new_token, same
from . import rate_limits as limits
from .audit import record

CODE_LIFETIME = timedelta(minutes=5)  # 验证码有效期
MAX_CODE_FAILURES = 5  # 每个验证请求最多输错 5 次
TOKEN_LIFETIME = timedelta(minutes=10)  # 注册凭证有效期
MAX_TOKEN_CHARS = 100


@dataclass(frozen=True)
class StartResult:
    """start() 的返回：请求编号，前端在第二步把它和验证码一起提交。"""

    challenge_id: UUID


def _code_hmac(keys: AuthKeys, challenge_id, email_key: str, code: str) -> bytes:
    # 验证码的校验值绑定了请求编号、邮箱和用途：同一个验证码换个请求就对不上。
    return fingerprint(
        keys.email_code, "register-code", str(challenge_id), email_key, code
    )


def flow_binding(keys: AuthKeys, flow_session_id) -> bytes:
    # 浏览器流程绑定：只有发起注册的那个匿名会话，才能继续完成后面两步。
    """根据浏览器匿名会话编号算出“流程绑定值”，存进验证请求；之后每一步都要对得上，
    别的浏览器拿到验证码或凭证也用不了。重置密码流程也复用它。"""
    return fingerprint(keys.session, "register-flow", str(flow_session_id))


def _token_hash(keys: AuthKeys, token: str) -> bytes:
    return fingerprint(keys.email_code, "register-token", token)


def start(
    engine,
    keys: AuthKeys,
    mailer: Mailer,
    raw_email: str,
    *,
    flow_session_id,
    source: str,
    request_id: str | None = None,
) -> StartResult:
    """第 1 步：申请注册验证码。邮箱格式不对抛 InvalidEmail，太频繁抛 RateLimited。"""
    email, email_key = normalize_email(raw_email)
    # 发送频率限制：同邮箱 60 秒 1 封、每小时 5 封；同 IP 每小时 20 封；全站每小时 200 封。
    checks = [
        (limits.REGISTER_RESEND, email_key),
        (limits.REGISTER_EMAIL, email_key),
        (limits.REGISTER_SOURCE, source),
        (limits.MAIL_TOTAL, "all"),
    ]
    with engine.begin() as connection:
        limits.ensure_allowed(connection, keys, *checks)
        for rule, *parts in checks:
            limits.count(connection, keys, rule, *parts)
        # 重新获取验证码时，这个邮箱之前所有没用完的注册请求（包括已验证、未完成的）全部作废。
        # 只作废“注册”用途的，不影响同一邮箱正在进行的重置密码。
        connection.execute(
            challenges.update()
            .where(
                challenges.c.purpose == "register",
                challenges.c.email_key == email_key,
                challenges.c.consumed_at.is_(None),
                challenges.c.invalidated_at.is_(None),
            )
            .values(invalidated_at=sa.func.now())
        )
        registered = connection.scalar(
            sa.select(users.c.user_id).where(users.c.email_key == email_key)
        )
        challenge_id = uuid4()
        code = None
        if registered is None:
            code = new_code()
            connection.execute(
                challenges.insert().values(
                    challenge_id=challenge_id,
                    email=email,
                    email_key=email_key,
                    purpose="register",
                    code_hmac=_code_hmac(keys, challenge_id, email_key, code),
                    key_version=keys.version,
                    flow_binding=flow_binding(keys, flow_session_id),
                    expires_at=sa.func.now() + CODE_LIFETIME,
                )
            )
        # 已注册的邮箱：请求编号照样返回，但数据库里没有这条请求，输入任何验证码都会失败。
        record(
            connection,
            "register_requested",
            source=source,
            request_id=request_id,
            reason="existing_account" if registered else None,
        )
    # 事务提交之后才发邮件：如果先发信、后提交失败，用户会收到一个根本用不了的验证码。
    try:
        if code is None:
            mailer.send_already_registered(email)
        else:
            mailer.send_registration_code(email, code)
    except MailDeliveryFailed:
        with engine.begin() as connection:
            record(
                connection,
                "register_mail_failed",
                source=source,
                request_id=request_id,
            )
        raise
    return StartResult(challenge_id)


def verify(
    engine,
    keys: AuthKeys,
    challenge_id,
    code: str,
    *,
    flow_session_id,
    source: str,
    request_id: str | None = None,
) -> str:
    """第 2 步：核对验证码，成功返回注册凭证原值（只返回这一次）。

    请求不存在、已作废、已用过、已过期、输错次数用完、不是同一浏览器、验证码不对，
    对外一律抛出 VerificationFailed；真实原因写进审计记录。
    """
    code = (code or "").strip()
    token = None
    reason = None
    with engine.begin() as connection:
        # FOR UPDATE：锁住这一行，同时提交的两个验证请求排队处理，验证码不能被用两次。
        row = connection.execute(
            sa.select(
                challenges,
                (challenges.c.expires_at <= sa.func.now()).label("expired"),
            )
            # 只找“注册”用途的请求：拿重置密码的请求编号来这里，等同于请求不存在。
            .where(
                challenges.c.challenge_id == challenge_id,
                challenges.c.purpose == "register",
            )
            .with_for_update(of=challenges)
        ).one_or_none()
        if row is None:
            reason = "unknown_challenge"
        else:
            wait = limits.retry_after(
                connection, keys, limits.VERIFY_EMAIL, row.email_key
            )
            if wait is not None:
                raise RateLimited(wait)
            if row.invalidated_at or row.consumed_at or row.verified_at:
                reason = "not_open"
            elif row.expired:
                reason = "expired"
            elif row.failed_attempts >= MAX_CODE_FAILURES:
                reason = "too_many_failures"
            elif not same(row.flow_binding, flow_binding(keys, flow_session_id)):
                reason = "other_flow"
            elif not (
                len(code) == CODE_DIGITS
                and code.isdigit()
                and same(
                    row.code_hmac,
                    _code_hmac(keys, row.challenge_id, row.email_key, code),
                )
            ):
                reason = "wrong_code"
                connection.execute(
                    challenges.update()
                    .where(challenges.c.challenge_id == row.challenge_id)
                    .values(failed_attempts=challenges.c.failed_attempts + 1)
                )
                limits.count(connection, keys, limits.VERIFY_EMAIL, row.email_key)
            else:
                token = new_token()
                connection.execute(
                    challenges.update()
                    .where(challenges.c.challenge_id == row.challenge_id)
                    .values(
                        verified_at=sa.func.now(),
                        registration_token_hash=_token_hash(keys, token),
                        registration_expires_at=sa.func.now() + TOKEN_LIFETIME,
                    )
                )
        record(
            connection,
            "register_verified" if token else "register_verify_failed",
            source=source,
            request_id=request_id,
            reason=reason,
        )
    # 失败时，输错次数和审计记录已经随事务提交，再对外报错。
    if token is None:
        raise VerificationFailed()
    return token


def _open_by_token(registration_token_hash: bytes):
    """“这个注册凭证仍然可用”的查询条件：已验证、未用掉、未作废、未过期。"""
    return sa.and_(
        challenges.c.purpose == "register",
        challenges.c.registration_token_hash == registration_token_hash,
        challenges.c.verified_at.is_not(None),
        challenges.c.consumed_at.is_(None),
        challenges.c.invalidated_at.is_(None),
        challenges.c.registration_expires_at > sa.func.now(),
    )


def complete(
    engine,
    keys: AuthKeys,
    registration_token: str,
    password: str,
    *,
    flow_session_id,
    source: str,
    request_id: str | None = None,
) -> UUID:
    """第 3 步：设置密码并创建账户，返回新账户的 user_id。

    密码不符合规则抛 PasswordRejected，这时凭证不会被用掉，用户可以换个密码重试。
    """
    if not registration_token or len(registration_token) > MAX_TOKEN_CHARS:
        raise RegistrationFailed()
    token_hash = _token_hash(keys, registration_token)

    # ① 先不加锁地读出这条请求，拿到邮箱（检查“密码不能等于邮箱”要用）。
    with engine.begin() as connection:
        row = connection.execute(
            sa.select(
                challenges.c.challenge_id,
                challenges.c.email,
                challenges.c.flow_binding,
            ).where(_open_by_token(token_hash))
        ).one_or_none()
    if row is None or not same(row.flow_binding, flow_binding(keys, flow_session_id)):
        raise RegistrationFailed()
    value = check_new_password(password, email=row.email)

    # ② 计算密码哈希比较慢（几十毫秒），放在事务外面，不让数据库锁等着它。
    password_hash = hash_password(value)

    # ③ 加锁后重新确认凭证仍可用，然后在同一个事务里：用掉凭证、创建账户、写审计。
    user_id = None
    with engine.begin() as connection:
        locked = connection.execute(
            sa.select(
                challenges.c.email, challenges.c.email_key, challenges.c.verified_at
            )
            .where(
                challenges.c.challenge_id == row.challenge_id,
                _open_by_token(token_hash),
            )
            .with_for_update()
        ).one_or_none()
        if locked is not None:
            connection.execute(
                challenges.update()
                .where(challenges.c.challenge_id == row.challenge_id)
                .values(consumed_at=sa.func.now())
            )
            # ON CONFLICT DO NOTHING：同一邮箱已经有账户时不插入、也不覆盖原账户的密码，
            # 由数据库唯一约束保证，而不是“先查有没有再插入”（那样并发时会出错）。
            user_id = connection.scalar(
                pg_insert(users)
                .values(
                    email=locked.email,
                    email_key=locked.email_key,
                    password_hash=password_hash,
                    email_verified_at=locked.verified_at,
                    password_changed_at=sa.func.now(),
                )
                .on_conflict_do_nothing(index_elements=["email_key"])
                .returning(users.c.user_id)
            )
        record(
            connection,
            "register_completed" if user_id else "register_complete_failed",
            user_id=user_id,
            source=source,
            request_id=request_id,
            reason=None
            if user_id
            else ("token_gone" if locked is None else "existing_account"),
        )
    if user_id is None:
        raise RegistrationFailed()
    return user_id
