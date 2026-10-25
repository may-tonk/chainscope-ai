"""CSRF 防护的“来源检查”部分（方案 7.3 节）。

CSRF 防护有两道检查，都通过才放行：
1. 来源检查（本文件）：请求头 Origin 必须是允许的网站地址；没有 Origin 时看 Referer；
   两者都没有就拒绝，不默认放行。
2. 令牌检查（services/sessions.py 的 csrf_matches）：请求头 X-CSRF-Token
   必须和当前会话的 CSRF 令牌一致。别的网站读不到我们接口的响应，所以拿不到这个令牌。

两道检查由接口层 api/dependencies.py 组合起来。本文件只依赖 core，
不调用 services，保持“api → services → security → core”的调用方向。

只读请求（GET）不做检查，所以所有修改数据的接口都必须用 POST。
"""

from urllib.parse import urlsplit

from ..core.errors import CsrfRejected

HEADER = "X-CSRF-Token"


def request_origin(headers) -> str | None:
    """取出请求来自哪个网站（协议://主机[:端口]）。优先用 Origin，没有再从 Referer 推出。"""
    origin = headers.get("origin")
    if origin and origin != "null":
        return origin.rstrip("/")
    referer = headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return None


def check_origin(headers, allowed_origins: tuple[str, ...]) -> None:
    """来源不在允许列表里（或无法判断来源）时抛出 CsrfRejected（接口返回 403）。"""
    if request_origin(headers) not in allowed_origins:
        raise CsrfRejected()
