"""账户操作：登录、退出、退出所有设备、修改密码、注销账户（方案第 6、7.1、13.1 节）。

几条共同原则：
- 对外失败统一是 AuthenticationFailed（“账号或密码错误”），真实原因只写进审计；
- 先检查限速，再做耗时的密码哈希；
- 密码哈希放在事务外面算（几十毫秒），避免数据库锁长时间等待；
- 真正修改数据之前，在事务里锁住账户行（FOR UPDATE），重新确认账户状态、密码哈希、
  认证版本和刚才读到的一样。这样“旧密码刚验证通过，另一个请求却已经改了密码”时，
  不会用旧密码换到新会话。
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa

from ..core.emails import normalize_email
from ..core.errors import AuthenticationFailed
from ..core.keys import AuthKeys
from ..db.tables import challenges, users
from ..security.passwords import (
    check_new_password,
    hash_password,
    verify_password,
    verify_unknown_user,
)
from . import rate_limits as limits
from .audit import record
from .sessions import IssuedSession, issue, revoke, revoke_all


@dataclass(frozen=True)
class LoginResult:
    user_id: UUID
    session: IssuedSession  # 新签发的已登录会话（令牌原值只在这里出现一次）


@dataclass(frozen=True)
class Account:
    """给“账户”页面显示的信息，不含密码哈希等敏感字段。"""

    user_id: UUID
    email: str
    created_at: datetime


# 读账户时需要的列；password_hash 只在后台比对，绝不返回给前端。
_ACCOUNT_COLUMNS = (
    users.c.user_id,
    users.c.email,
    users.c.email_key,
    users.c.password_hash,
    users.c.status,
    users.c.auth_version,
)


def _lock_unchanged(connection, user):
    """锁住账户行，并确认它和之前读到的一样（状态、密码哈希、认证版本都没变）。"""
    current = connection.execute(
        sa.select(users.c.password_hash, users.c.status, users.c.auth_version)
        .where(users.c.user_id == user.user_id)
        .with_for_update()
    ).one_or_none()
    return (
        current is not None
        and current.status == "active"
        and current.password_hash == user.password_hash
        and current.auth_version == user.auth_version
    )


def login(
    engine,
    keys: AuthKeys,
    raw_email: str,
    password: str,
    *,
    source: str,
    request_id: str | None = None,
) -> LoginResult:
    """邮箱＋密码登录，成功返回新的已登录会话。"""
    email, email_key = normalize_email(raw_email)

    # ① 限速检查（三条规则），记一次来源尝试，读出账户。
    with engine.begin() as connection:
        limits.ensure_allowed(
            connection,
            keys,
            (limits.LOGIN_SOURCE, source),
            (limits.LOGIN_EMAIL_SOURCE, email_key, source),
            (limits.LOGIN_EMAIL, email_key),
        )
        limits.count(connection, keys, limits.LOGIN_SOURCE, source)
        user = connection.execute(
            sa.select(*_ACCOUNT_COLUMNS).where(users.c.email_key == email_key)
        ).one_or_none()

    # ② 校验密码（事务外）。邮箱不存在也陪跑一次哈希，让响应时间差不多。
    if user is None:
        ok, new_hash = verify_unknown_user(password), None
    else:
        ok, new_hash = verify_password(password, user.password_hash)

    # ③ 失败：记失败次数和审计，然后统一报“账号或密码错误”。
    if not ok or user.status != "active":
        if user is None:
            reason = "unknown_email"
        else:
            reason = "disabled" if ok else "wrong_password"
        with engine.begin() as connection:
            limits.count(connection, keys, limits.LOGIN_EMAIL_SOURCE, email_key, source)
            limits.count(connection, keys, limits.LOGIN_EMAIL, email_key)
            record(
                connection,
                "login_failed",
                user_id=user.user_id if user else None,
                source=source,
                request_id=request_id,
                reason=reason,
            )
        raise AuthenticationFailed()

    # ④ 成功：锁住账户确认没变，再签发会话。会话提交成功后才把令牌交给调用方。
    issued = None
    with engine.begin() as connection:
        if _lock_unchanged(connection, user):
            if new_hash:
                # 旧参数生成的哈希：趁密码正确，换成新参数的哈希（不改变认证版本）。
                connection.execute(
                    users.update()
                    .where(users.c.user_id == user.user_id)
                    .values(password_hash=new_hash, updated_at=sa.func.now())
                )
            issued = issue(connection, keys, "user", user.user_id, user.auth_version)
            # 真实用户登录成功，清掉“这个来源对这个邮箱”的失败计数。
            limits.clear(connection, keys, limits.LOGIN_EMAIL_SOURCE, email_key, source)
        record(
            connection,
            "login_succeeded" if issued else "login_failed",
            user_id=user.user_id,
            source=source,
            request_id=request_id,
            reason=None if issued else "changed_during_login",
        )
    if issued is None:
        raise AuthenticationFailed()
    return LoginResult(user.user_id, issued)


def logout(
    engine, session_id, user_id, *, source: str | None = None, request_id=None
) -> None:
    """退出当前设备：撤销这一个会话，其他设备不受影响。"""
    with engine.begin() as connection:
        revoke(connection, session_id)
        record(
            connection, "logout", user_id=user_id, source=source, request_id=request_id
        )


def _verify_current_password(
    engine, keys: AuthKeys, user_id, password: str, action: str, source, request_id
):
    """已登录后的敏感操作前，核对当前密码（带限速）。通过返回账户行，否则抛 AuthenticationFailed。"""
    with engine.begin() as connection:
        limits.ensure_allowed(connection, keys, (limits.ACCOUNT_PASSWORD, user_id))
        user = connection.execute(
            sa.select(*_ACCOUNT_COLUMNS).where(users.c.user_id == user_id)
        ).one_or_none()
    if user is None or user.status != "active":
        raise AuthenticationFailed()
    ok, _ = verify_password(password, user.password_hash)
    if not ok:
        with engine.begin() as connection:
            limits.count(connection, keys, limits.ACCOUNT_PASSWORD, user_id)
            record(
                connection,
                f"{action}_failed",
                user_id=user_id,
                source=source,
                request_id=request_id,
                reason="wrong_password",
            )
        raise AuthenticationFailed()
    return user


def logout_everywhere(
    engine,
    keys: AuthKeys,
    user_id,
    current_password: str,
    *,
    source: str,
    request_id: str | None = None,
) -> None:
    """退出所有设备：核对当前密码后，认证版本加 1，并撤销该用户全部会话。"""
    user = _verify_current_password(
        engine, keys, user_id, current_password, "logout_all", source, request_id
    )
    with engine.begin() as connection:
        if not _lock_unchanged(connection, user):
            raise AuthenticationFailed()
        connection.execute(
            users.update()
            .where(users.c.user_id == user_id)
            .values(auth_version=users.c.auth_version + 1, updated_at=sa.func.now())
        )
        revoke_all(connection, user_id)
        record(
            connection,
            "logout_all",
            user_id=user_id,
            source=source,
            request_id=request_id,
        )


def change_password(
    engine,
    keys: AuthKeys,
    user_id,
    current_password: str,
    new_password: str,
    *,
    source: str,
    request_id: str | None = None,
) -> None:
    """修改密码：核对当前密码，新密码符合规则后保存；之后所有设备（包括当前设备）都要重新登录。"""
    user = _verify_current_password(
        engine, keys, user_id, current_password, "password_change", source, request_id
    )
    value = check_new_password(new_password, email=user.email)
    new_hash = hash_password(value)
    with engine.begin() as connection:
        if not _lock_unchanged(connection, user):
            raise AuthenticationFailed()
        # 改密码和撤销旧会话在同一个事务里：要么都生效，要么都不生效。
        connection.execute(
            users.update()
            .where(users.c.user_id == user_id)
            .values(
                password_hash=new_hash,
                password_changed_at=sa.func.now(),
                auth_version=users.c.auth_version + 1,
                updated_at=sa.func.now(),
            )
        )
        revoke_all(connection, user_id)
        # 之前申请过、还没完成的重置密码请求一起作废（密码已经换了，旧请求不该再生效）。
        connection.execute(
            challenges.update()
            .where(
                challenges.c.user_id == user_id,
                challenges.c.consumed_at.is_(None),
                challenges.c.invalidated_at.is_(None),
            )
            .values(invalidated_at=sa.func.now())
        )
        limits.clear(connection, keys, limits.ACCOUNT_PASSWORD, user_id)
        record(
            connection,
            "password_changed",
            user_id=user_id,
            source=source,
            request_id=request_id,
        )


def delete_account(
    engine,
    keys: AuthKeys,
    user_id,
    current_password: str,
    *,
    source: str,
    request_id: str | None = None,
) -> None:
    """注销账户（立即彻底删除，方案 13.1）：核对当前密码后，在一个事务里

    - 作废这个邮箱还没完成的注册请求；
    - 删除账户行：它的全部会话由数据库外键自动删除，审计记录里的 user_id 自动清空；
    - 写一条不含用户编号的审计事件。

    删除后同一个邮箱可以重新注册，新账户不继承任何旧数据。
    """
    user = _verify_current_password(
        engine, keys, user_id, current_password, "account_delete", source, request_id
    )
    with engine.begin() as connection:
        if not _lock_unchanged(connection, user):
            raise AuthenticationFailed()
        connection.execute(
            challenges.update()
            .where(
                challenges.c.email_key == user.email_key,
                challenges.c.consumed_at.is_(None),
                challenges.c.invalidated_at.is_(None),
            )
            .values(invalidated_at=sa.func.now())
        )
        connection.execute(users.delete().where(users.c.user_id == user_id))
        record(connection, "account_deleted", source=source, request_id=request_id)


def get_account(engine, user_id) -> Account | None:
    """读取账户页面要显示的信息。"""
    with engine.begin() as connection:
        row = connection.execute(
            sa.select(users.c.user_id, users.c.email, users.c.created_at).where(
                users.c.user_id == user_id
            )
        ).one_or_none()
    return None if row is None else Account(row.user_id, row.email, row.created_at)
