"""会话 Cookie 的设置和清除，4 个安全属性集中在这里，所有接口都用同一套设置（方案 7.2 节）。

- HttpOnly：网页里的 JavaScript 读不到，被插入恶意脚本也偷不走；
- Secure：只通过加密连接发送（浏览器把 localhost 视为安全环境，所以开发时也能用）；
- SameSite=Lax：从别的网站发起的大多数请求不会带上它；
- 名字带 __Host- 前缀：浏览器强制要求 Secure、Path=/、不能设置 Domain，子域名无法篡改。

匿名会话和已登录会话共用这一个名字：登录成功时用新令牌替换，浏览器里始终只有一张通行证。
Cookie 的有效期不超过服务端会话的有效期；到期后即使浏览器还留着，服务端也不认。
"""

from datetime import timedelta

from ..services.sessions import ANONYMOUS_ABSOLUTE, USER_ABSOLUTE

COOKIE_NAME = "__Host-chainscope_session"
_ATTRIBUTES = {"path": "/", "secure": True, "httponly": True, "samesite": "lax"}


def set_session_cookie(response, token: str, kind: str) -> None:
    """把会话令牌写进 Cookie。有效期跟着会话类型走：匿名 2 小时，已登录 7 天。"""
    lifetime: timedelta = USER_ABSOLUTE if kind == "user" else ANONYMOUS_ABSOLUTE
    response.set_cookie(
        COOKIE_NAME, token, max_age=int(lifetime.total_seconds()), **_ATTRIBUTES
    )


def clear_session_cookie(response) -> None:
    """让浏览器删除 Cookie。只是辅助：真正让令牌失效的是服务端撤销会话。"""
    response.delete_cookie(COOKIE_NAME, **_ATTRIBUTES)
