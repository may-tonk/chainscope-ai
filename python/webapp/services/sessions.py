"""服务端会话：签发、查验、撤销（方案 7.1 节）。

浏览器拿到的是一串随机令牌（以后放在 HttpOnly Cookie 里），
数据库 auth_sessions 只存它的 HMAC 指纹。每次请求带着令牌来，后台算指纹去查表。
因为状态在服务端，退出登录、改密码时可以立刻让旧令牌失效，不用等它自己过期。

两种会话：
- anonymous（匿名）：登录前的注册、登录页面用，主要用来做 CSRF 保护、绑定注册流程，2 小时过期；
- user（已登录）：登录成功后新签发（不沿用匿名会话），7 天绝对过期，24 小时无操作也会失效。

已登录会话每次查验还要核对账户：账户被禁用，或者 auth_version 变了（改了密码、退出了所有设备），
旧会话立即无效。

CSRF 令牌（2b 调整）：不是单独随机生成，而是用 AUTH_CSRF_KEY 对会话编号算 HMAC 得到。
服务器随时能重新算出同一个值，所以用户刷新页面后，前端调用一次接口就能重新取回；
没有密钥的人算不出来，别的网站也读不到这个接口的响应。数据库里仍只存它的指纹。
"""

import base64
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa

from ..core.keys import AuthKeys
from ..db.tables import sessions, users
from ..security.tokens import fingerprint, new_token, same
from . import rate_limits as limits

USER_ABSOLUTE = timedelta(days=7)  # 已登录会话最长有效期，到点就失效
USER_IDLE = timedelta(hours=24)  # 已登录会话连续这么久没有操作也失效
ANONYMOUS_ABSOLUTE = timedelta(hours=2)  # 匿名会话有效期，足够完成一次注册
# 最近活动时间不是每个请求都写：距上次记录超过 5 分钟才更新，减少数据库写入。
TOUCH_EVERY = timedelta(minutes=5)
MAX_TOKEN_CHARS = 100  # 正常令牌约 43 个字符；明显过长的直接当无效，不去查库


@dataclass(frozen=True)
class IssuedSession:
    """刚签发的会话。登录令牌 token 只在这一刻以原值存在，之后数据库里只有指纹；
    csrf_token 可以随时用 csrf_token_for() 重新算出。"""

    session_id: UUID
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)


@dataclass(frozen=True)
class SessionInfo:
    """查验通过的会话。"""

    session_id: UUID
    kind: str  # "anonymous" 或 "user"
    user_id: UUID | None
    csrf_hash: bytes = field(repr=False)


def token_hash(keys: AuthKeys, token: str) -> bytes:
    """登录令牌的指纹：数据库里只存这个，不存令牌原值。"""
    return fingerprint(keys.session, "session", token)


def csrf_hash(keys: AuthKeys, csrf_token: str) -> bytes:
    """CSRF 令牌的指纹：查验时把浏览器提交的令牌再算一次，和数据库里的比对。"""
    return fingerprint(keys.csrf, "csrf", csrf_token)


def csrf_token_for(keys: AuthKeys, session_id) -> str:
    """根据会话编号算出这个会话的 CSRF 令牌（base64url 文本，约 43 个字符）。"""
    raw = fingerprint(keys.csrf, "csrf-token", str(session_id))
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def issue(
    connection, keys: AuthKeys, kind: str, user_id=None, auth_version=None
) -> IssuedSession:
    """在调用方的事务里新建一个会话，返回令牌原值（只返回这一次）。

    会话编号在这里用 uuid4（操作系统安全随机源）生成，因为 CSRF 令牌要由它算出来。
    """
    session_id = uuid4()
    token, csrf_token = new_token(), csrf_token_for(keys, session_id)
    lifetime = USER_ABSOLUTE if kind == "user" else ANONYMOUS_ABSOLUTE
    connection.execute(
        sessions.insert().values(
            session_id=session_id,
            kind=kind,
            token_hash=token_hash(keys, token),
            key_version=keys.version,
            user_id=user_id,
            auth_version=auth_version,
            csrf_hash=csrf_hash(keys, csrf_token),
            absolute_expires_at=sa.func.now() + lifetime,
        )
    )
    return IssuedSession(session_id, token, csrf_token)


def start_anonymous(engine, keys: AuthKeys, source: str | None = None) -> IssuedSession:
    """新建一个匿名会话（打开注册或登录页面时用）。

    传入 source 时按来源限速（每小时 60 个），防止有人不停新建匿名会话把表撑满。
    """
    with engine.begin() as connection:
        if source is not None:
            limits.ensure_allowed(connection, keys, (limits.ANONYMOUS_SESSION, source))
            limits.count(connection, keys, limits.ANONYMOUS_SESSION, source)
        return issue(connection, keys, "anonymous")


def lookup(engine, keys: AuthKeys, token: str | None) -> SessionInfo | None:
    """查验浏览器带来的令牌。无效、过期、已撤销、账户已禁用或版本不符，一律返回 None。"""
    if not token or len(token) > MAX_TOKEN_CHARS:
        return None
    now = sa.func.now()
    # 已登录会话的额外条件：未空闲超时、账户仍是 active、认证版本没变。
    user_ok = sa.and_(
        sessions.c.last_seen_at > now - USER_IDLE,
        users.c.status == "active",
        users.c.auth_version == sessions.c.auth_version,
    )
    query = (
        sa.select(
            sessions.c.session_id,
            sessions.c.kind,
            sessions.c.user_id,
            sessions.c.csrf_hash,
            (sessions.c.last_seen_at < now - TOUCH_EVERY).label("touch_due"),
        )
        .select_from(sessions.outerjoin(users, users.c.user_id == sessions.c.user_id))
        .where(
            sessions.c.token_hash == token_hash(keys, token),
            # 换过密钥（版本号变了）以后，旧版本签发的会话全部作废。
            sessions.c.key_version == keys.version,
            sessions.c.revoked_at.is_(None),
            sessions.c.absolute_expires_at > now,
            sa.or_(sessions.c.kind == "anonymous", user_ok),
        )
    )
    with engine.begin() as connection:
        row = connection.execute(query).one_or_none()
        if row is None:
            return None
        if row.touch_due:
            connection.execute(
                sessions.update()
                .where(sessions.c.session_id == row.session_id)
                .values(last_seen_at=now)
            )
    return SessionInfo(row.session_id, row.kind, row.user_id, row.csrf_hash)


def csrf_matches(keys: AuthKeys, session: SessionInfo, presented: str | None) -> bool:
    """浏览器提交的 CSRF 令牌是否属于这个会话（2b 的接口层会在每个修改操作前调用）。"""
    if not presented or len(presented) > MAX_TOKEN_CHARS:
        return False
    return same(csrf_hash(keys, presented), session.csrf_hash)


def revoke(connection, session_id) -> None:
    """撤销一个会话（退出当前设备）。只标记撤销时间，复制走的旧令牌也立即无效。"""
    connection.execute(
        sessions.update()
        .where(sessions.c.session_id == session_id, sessions.c.revoked_at.is_(None))
        .values(revoked_at=sa.func.now())
    )


def revoke_all(connection, user_id) -> None:
    """撤销某个用户的全部会话（改密码、退出所有设备）。"""
    connection.execute(
        sessions.update()
        .where(sessions.c.user_id == user_id, sessions.c.revoked_at.is_(None))
        .values(revoked_at=sa.func.now())
    )
