"""接口共用的准备和检查（FastAPI 的 Depends 依赖）。

接口函数在参数里写 `session = Depends(require_user_csrf)`，FastAPI 就会在执行接口之前
先运行这里的检查；检查不通过直接抛错，接口函数根本不会被执行。
同一个请求里，同一个依赖只运行一次（例如 current_session 不会重复查库）。

| 依赖 | 作用 |
| --- | --- |
| web_context      | 取出数据库连接池、密钥、运行配置、发信器 |
| request_id       | 本次请求的编号（app.py 的中间件生成） |
| client_source    | 请求来源 IP；只有经过受信任代理时才读取 X-Forwarded-For |
| current_session  | 读 Cookie 并查验会话；没有或无效返回 None |
| require_csrf     | 修改操作必须通过来源检查和 CSRF 令牌检查 |
| require_user     | 必须已登录 |
| require_user_csrf| 已登录，并且通过 CSRF 检查 |
"""

import ipaddress
from dataclasses import dataclass

from fastapi import Depends, Request

from ..core.app_config import AppConfig
from ..core.errors import CsrfRejected, NotAuthenticated
from ..core.keys import AuthKeys
from ..security.csrf import HEADER, check_origin
from ..services.sessions import SessionInfo, csrf_matches, lookup
from .cookies import COOKIE_NAME


@dataclass(frozen=True)
class WebContext:
    """整个网站后端共用的对象，在 app.py 启动时创建一次。"""

    engine: object
    keys: AuthKeys
    config: AppConfig
    mailer: object


def web_context(request: Request) -> WebContext:
    """取出启动时创建的共用对象（数据库、密钥、配置、发信器）。"""
    return request.app.state.webapp


def request_id(request: Request) -> str:
    """本次请求的编号，由 app.py 的中间件在请求进来时生成，写进审计和错误响应。"""
    return request.state.request_id


def client_source(request: Request) -> str:
    """请求来源 IP，用于限速和审计。

    X-Forwarded-For 这个头任何人都能随便填，如果无条件相信它，攻击者每次换一个假 IP
    就能绕过按 IP 的限速。所以只有当直接连过来的是我们配置的受信任代理时，才读取它，
    并从右往左找到第一个不属于受信任代理的地址，那才是真正的用户地址。
    """
    proxies = web_context(request).config.trusted_proxies
    peer = request.client.host if request.client else ""
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer or "unknown"
    if proxies and any(peer_ip in net for net in proxies):
        forwarded = request.headers.get("x-forwarded-for", "")
        for item in reversed(
            [part.strip() for part in forwarded.split(",") if part.strip()]
        ):
            try:
                hop = ipaddress.ip_address(item)
            except ValueError:
                break  # 格式不对就不再相信这个头，退回用直接连接的地址
            if not any(hop in net for net in proxies):
                return str(hop)
    return str(peer_ip)


def current_session(request: Request) -> SessionInfo | None:
    """读取浏览器带来的会话 Cookie 并查验；没有或无效时返回 None（不报错，由调用方决定怎么处理）。"""
    context = web_context(request)
    return lookup(context.engine, context.keys, request.cookies.get(COOKIE_NAME))


def ensure_csrf(request: Request, session: SessionInfo | None) -> SessionInfo:
    """来源检查 + CSRF 令牌检查，都通过才返回会话。"""
    context = web_context(request)
    check_origin(request.headers, context.config.allowed_origins)
    if session is None or not csrf_matches(
        context.keys, session, request.headers.get(HEADER)
    ):
        raise CsrfRejected()
    return session


def require_csrf(
    request: Request, session: SessionInfo | None = Depends(current_session)
) -> SessionInfo:
    """修改操作的依赖：必须通过来源检查和 CSRF 令牌检查，否则返回 403。"""
    return ensure_csrf(request, session)


def require_user(
    session: SessionInfo | None = Depends(current_session),
) -> SessionInfo:
    """需要登录的只读接口的依赖：没有已登录会话时返回 401。"""
    if session is None or session.kind != "user":
        raise NotAuthenticated()
    return session


def require_user_csrf(session: SessionInfo = Depends(require_csrf)) -> SessionInfo:
    """需要登录的修改操作的依赖：先过 CSRF 检查，再确认是已登录会话。"""
    if session.kind != "user":
        raise NotAuthenticated()
    return session
