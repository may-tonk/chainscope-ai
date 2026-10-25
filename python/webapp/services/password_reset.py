"""通过邮箱重置密码（2c，方案 2.3 节）。流程和注册一样分三步，都绑定发起重置的那个浏览器：

1. start()：提交邮箱。邮箱有正常账户时生成 6 位验证码；不存在或已被禁用时什么都不生成。
   两种情况返回的内容完全一样。邮件由接口层在回复之后再发送（deliver_code），
   这样“有账户”和“没账户”的响应速度一样，别人没法靠快慢判断哪个邮箱注册过。
2. verify()：提交验证码，换取 10 分钟有效、只能用一次的“重置凭证”。
3. complete()：凭重置凭证设置新密码。在一个事务里：
   - 更新密码哈希，auth_version 加 1；
   - 撤销这个账户全部设备上的登录（只是让通行证作废）；
   - 用掉这个重置请求，作废同一账户其他未完成的重置请求；
   - 清掉“这个来源对这个邮箱”的登录失败冷却，让用户马上能用新密码登录。

重置不会删除或改动账户本身：用户编号、邮箱、注册时间和账户下的所有内容都保留。
只有用户主动注销账户时才会删除账户（见 accounts.delete_account）。
"""

from dataclasses import dataclass, field
from uuid import UUID, uuid4

import sqlalchemy as sa

from ..core.emails import normalize_email
from ..core.errors import (
    MailDeliveryFailed,
    PasswordResetFailed,
    RateLimited,
    VerificationFailed,
)
from ..core.keys import AuthKeys
from ..core.mailer import Mailer
from ..db.tables import challenges, users
from ..security.passwords import check_new_password, hash_password
from ..security.tokens import CODE_DIGITS, fingerprint, new_code, new_token, same
from . import rate_limits as limits
from .audit import record
from .registration import (
    CODE_LIFETIME,
    MAX_CODE_FAILURES,
    MAX_TOKEN_CHARS,
    TOKEN_LIFETIME,
    flow_binding,
)
from .sessions import revoke_all

PURPOSE = "password_reset"


@dataclass(frozen=True)
class ResetStart:
    """start() 的结果。challenge_id 返回给前端；email 和 code 只交给后台发信，不返回给前端。

    邮箱没有正常账户时 email 和 code 都是 None，后台什么也不发。
    """

    challenge_id: UUID
    email: str | None = None
    code: str | None = field(default=None, repr=False)


def _code_hmac(keys: AuthKeys, challenge_id, email_key: str, code: str) -> bytes:
    # 用途 "reset-code" 和注册的 "register-code" 不同：两种验证码不能互相冒用。
    return fingerprint(
        keys.email_code, "reset-code", str(challenge_id), email_key, code
    )


def _token_hash(keys: AuthKeys, token: str) -> bytes:
    return fingerprint(keys.email_code, "reset-token", token)


def _invalidate_open(connection, *conditions) -> None:
    """作废还没用掉的重置请求。"""
    connection.execute(
        challenges.update()
        .where(
            challenges.c.purpose == PURPOSE,
            challenges.c.consumed_at.is_(None),
            challenges.c.invalidated_at.is_(None),
            *conditions,
        )
        .values(invalidated_at=sa.func.now())
    )


def start(
    engine,
    keys: AuthKeys,
    raw_email: str,
    *,
    flow_session_id,
    source: str,
    request_id: str | None = None,
) -> ResetStart:
    """第 1 步：申请重置验证码。邮箱格式不对抛 InvalidEmail，太频繁抛 RateLimited。"""
    email, email_key = normalize_email(raw_email)
    # 不管邮箱有没有账户都计数：否则“有账户才会被限速”本身就泄露了信息。
    checks = [
        (limits.RESET_RESEND, email_key),
        (limits.RESET_EMAIL, email_key),
        (limits.RESET_SOURCE, source),
        (limits.MAIL_TOTAL, "all"),
    ]
    with engine.begin() as connection:
        limits.ensure_allowed(connection, keys, *checks)
        for rule, *parts in checks:
            limits.count(connection, keys, rule, *parts)
        _invalidate_open(connection, challenges.c.email_key == email_key)
        user = connection.execute(
            sa.select(users.c.user_id, users.c.email, users.c.status).where(
                users.c.email_key == email_key
            )
        ).one_or_none()
        challenge_id = uuid4()
        code = None
        if user is not None and user.status == "active":
            code = new_code()
            connection.execute(
                challenges.insert().values(
                    challenge_id=challenge_id,
                    email=user.email,
                    email_key=email_key,
                    purpose=PURPOSE,
                    user_id=user.user_id,
                    code_hmac=_code_hmac(keys, challenge_id, email_key, code),
                    key_version=keys.version,
                    flow_binding=flow_binding(keys, flow_session_id),
                    expires_at=sa.func.now() + CODE_LIFETIME,
                )
            )
        if user is None:
            reason = "unknown_email"
        elif code is None:
            reason = "disabled"
        else:
            reason = None
        record(
            connection,
            "password_reset_requested",
            user_id=user.user_id if code else None,
            source=source,
            request_id=request_id,
            reason=reason,
        )
    if code is None:
        return ResetStart(challenge_id)
    return ResetStart(challenge_id, user.email, code)


def deliver_code(
    engine, mailer: Mailer, started: ResetStart, *, source=None, request_id=None
) -> None:
    """在回复之后由接口层的后台任务调用：真正发送重置验证码。

    发送失败不能告诉页面（那会暴露“这个邮箱有账户”），只写进审计；用户可以稍后重新获取。
    """
    if started.code is None:
        return
    try:
        mailer.send_password_reset_code(started.email, started.code)
    except MailDeliveryFailed:
        with engine.begin() as connection:
            record(
                connection,
                "password_reset_mail_failed",
                source=source,
                request_id=request_id,
            )


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
    """第 2 步：核对验证码，成功返回重置凭证原值（只返回这一次）。

    请求不存在、已作废、已用过、已过期、输错次数用完、不是同一浏览器、验证码不对，
    对外一律抛出 VerificationFailed；真实原因写进审计。
    """
    code = (code or "").strip()
    token = None
    reason = None
    user_id = None
    with engine.begin() as connection:
        row = connection.execute(
            sa.select(
                challenges,
                (challenges.c.expires_at <= sa.func.now()).label("expired"),
            )
            .where(
                challenges.c.challenge_id == challenge_id,
                challenges.c.purpose == PURPOSE,
            )
            .with_for_update(of=challenges)
        ).one_or_none()
        if row is None:
            reason = "unknown_challenge"
        else:
            user_id = row.user_id
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
            "password_reset_verified" if token else "password_reset_verify_failed",
            user_id=user_id,
            source=source,
            request_id=request_id,
            reason=reason,
        )
    if token is None:
        raise VerificationFailed()
    return token


def _open_by_token(token_hash: bytes):
    """“这个重置凭证仍然可用”的查询条件：已验证、未用掉、未作废、未过期。"""
    return sa.and_(
        challenges.c.purpose == PURPOSE,
        challenges.c.registration_token_hash == token_hash,
        challenges.c.verified_at.is_not(None),
        challenges.c.consumed_at.is_(None),
        challenges.c.invalidated_at.is_(None),
        challenges.c.registration_expires_at > sa.func.now(),
    )


def complete(
    engine,
    keys: AuthKeys,
    reset_token: str,
    new_password: str,
    *,
    flow_session_id,
    source: str,
    request_id: str | None = None,
) -> str:
    """第 3 步：设置新密码。成功返回账户邮箱（接口层据此在后台发“密码已修改”通知）。

    新密码不符合规则抛 PasswordRejected，这时凭证不会被用掉，可以换个密码重试。
    凭证无效、账户已不存在或被禁用，抛 PasswordResetFailed。
    """
    if not reset_token or len(reset_token) > MAX_TOKEN_CHARS:
        raise PasswordResetFailed()
    token_hash = _token_hash(keys, reset_token)

    # ① 先不加锁地读出请求，拿到账户邮箱（检查“密码不能等于邮箱”要用）。
    with engine.begin() as connection:
        row = connection.execute(
            sa.select(
                challenges.c.challenge_id,
                challenges.c.user_id,
                challenges.c.email,
                challenges.c.email_key,
                challenges.c.flow_binding,
            ).where(_open_by_token(token_hash))
        ).one_or_none()
    if row is None or not same(row.flow_binding, flow_binding(keys, flow_session_id)):
        raise PasswordResetFailed()
    value = check_new_password(new_password, email=row.email)

    # ② 计算新密码哈希（较慢），放在事务外。
    password_hash = hash_password(value)

    # ③ 加锁后重新确认凭证和账户都还有效，然后一次性完成全部修改。
    done = False
    with engine.begin() as connection:
        # 先锁账户、再锁请求，和“登录状态下改密码”的加锁顺序一致，
        # 两个操作同时发生时不会互相等待对方而卡死（死锁）。
        account = connection.execute(
            sa.select(users.c.user_id)
            .where(users.c.user_id == row.user_id, users.c.status == "active")
            .with_for_update()
        ).one_or_none()
        locked = connection.execute(
            sa.select(challenges.c.challenge_id)
            .where(
                challenges.c.challenge_id == row.challenge_id,
                _open_by_token(token_hash),
            )
            .with_for_update()
        ).one_or_none()
        if locked is not None and account is not None:
            # 只改密码相关的字段；用户编号、邮箱、注册时间和账户内容都不动。
            connection.execute(
                users.update()
                .where(users.c.user_id == row.user_id)
                .values(
                    password_hash=password_hash,
                    password_changed_at=sa.func.now(),
                    auth_version=users.c.auth_version + 1,
                    updated_at=sa.func.now(),
                )
            )
            revoke_all(connection, row.user_id)
            connection.execute(
                challenges.update()
                .where(challenges.c.challenge_id == row.challenge_id)
                .values(consumed_at=sa.func.now())
            )
            _invalidate_open(connection, challenges.c.user_id == row.user_id)
            limits.clear(
                connection, keys, limits.LOGIN_EMAIL_SOURCE, row.email_key, source
            )
            limits.clear(connection, keys, limits.ACCOUNT_PASSWORD, row.user_id)
            done = True
        record(
            connection,
            "password_reset_completed" if done else "password_reset_complete_failed",
            user_id=row.user_id if account is not None else None,
            source=source,
            request_id=request_id,
            reason=None
            if done
            else ("token_gone" if locked is None else "account_gone"),
        )
    if not done:
        raise PasswordResetFailed()
    return row.email


def deliver_changed_notice(
    engine, mailer: Mailer, email: str, *, source=None, request_id=None
) -> None:
    """重置成功后由后台任务调用：通知账户邮箱“密码已被重置”。万一不是本人操作，用户能马上发现。"""
    try:
        mailer.send_password_changed(email)
    except MailDeliveryFailed:
        with engine.begin() as connection:
            record(
                connection,
                "password_changed_mail_failed",
                source=source,
                request_id=request_id,
            )
