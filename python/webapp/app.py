"""FastAPI 入口：把配置、数据库、密钥、发信器和接口组装成一个网站后端。

启动方式（开发环境，只在本机 127.0.0.1:8000 监听）：

    python python/tools/run_webapp.py

这里用“工厂函数” create_app() 而不是在导入时就创建 app：
导入本文件不会立刻读 .env、连数据库，测试时可以传入测试库和临时密钥。

本文件还负责三件全局的事：
1. 每个请求生成请求编号，并加上安全响应头；
2. 只接受允许的 Host 请求头（防伪造主机名）；
3. 把业务异常统一转换成 HTTP 状态码和 {code, message, request_id} 格式，
   任何错误都不回显用户输入、不暴露服务器内部信息。
"""

import logging
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .api.dependencies import WebContext
from .api.routes_auth import router as auth_router
from .api.routes_password_reset import router as password_reset_router
from .core.app_config import AppConfig, load_app_config
from .core.errors import (
    AuthenticationFailed,
    ConfigError,
    CsrfRejected,
    DatabaseError,
    InvalidEmail,
    MailDeliveryFailed,
    NotAuthenticated,
    PasswordRejected,
    PasswordResetFailed,
    RateLimited,
    RegistrationFailed,
    ServiceBusy,
    VerificationFailed,
    WebappError,
)
from .core.keys import AuthKeys, load_keys
from .core.mailer import mailer_for
from .core.settings import load_profile
from .db.database import engine_for

log = logging.getLogger("webapp")

# 业务异常 → (HTTP 状态码, 固定的错误编号)。
ERRORS = {
    InvalidEmail: (400, "invalid_email"),
    VerificationFailed: (400, "verification_failed"),
    RegistrationFailed: (400, "registration_failed"),
    PasswordResetFailed: (400, "password_reset_failed"),
    AuthenticationFailed: (401, "authentication_failed"),
    NotAuthenticated: (401, "not_authenticated"),
    CsrfRejected: (403, "csrf_failed"),
    RateLimited: (429, "rate_limited"),
    ServiceBusy: (503, "busy"),
    MailDeliveryFailed: (503, "mail_failed"),
    DatabaseError: (503, "service_unavailable"),
}

# 只用于接口响应的安全头：接口只返回 JSON，不应该被当作网页显示，也不能被嵌进别的网站。
API_HEADERS = {
    "Cache-Control": "no-store",  # 认证响应不能被浏览器或代理缓存
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}
COMMON_HEADERS = {
    "X-Content-Type-Options": "nosniff",  # 不让浏览器“猜”内容类型
    "X-Frame-Options": "DENY",  # 不允许被嵌入 iframe（防点击劫持）
    "Referrer-Policy": "no-referrer",
}


def _error(request: Request, status: int, code: str, message: str, headers=None):
    body = {
        "code": code,
        "message": message,
        "request_id": getattr(request.state, "request_id", ""),
    }
    return JSONResponse(body, status_code=status, headers=headers)


def create_app(
    *,
    engine=None,
    keys: AuthKeys | None = None,
    config: AppConfig | None = None,
    mailer=None,
) -> FastAPI:
    """创建网站后端。参数都不传时从 .env 读取；测试时传入测试库、临时密钥等。"""
    config = config or load_app_config()
    keys = keys or load_keys()
    if engine is None:
        if config.is_production:
            # 生产环境的数据库配置在部署阶段增加，现在不允许用开发库冒充。
            raise ConfigError("生产环境的账户库配置尚未实现")
        engine = engine_for(load_profile("development"))
    mailer = mailer or mailer_for(config)

    app = FastAPI(
        title="ChainScope Web API",
        docs_url="/docs" if config.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.docs_enabled else None,
    )
    app.state.webapp = WebContext(engine, keys, config, mailer)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(config.allowed_hosts))

    @app.middleware("http")
    async def request_id_and_headers(request: Request, call_next):
        """每个请求都经过这里：先生成请求编号，处理完后在响应上加请求编号和安全响应头。"""
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        headers = {**COMMON_HEADERS}
        if request.url.path.startswith("/api/"):
            headers.update(API_HEADERS)
        for name, value in headers.items():
            response.headers[name] = value
        return response

    @app.exception_handler(WebappError)
    async def webapp_error(request: Request, exc: WebappError):
        """把业务异常（WebappError 及其子类）转换成对应的 HTTP 状态码和统一错误格式。"""
        if isinstance(exc, PasswordRejected):
            return _error(request, 400, exc.code, str(exc))
        for kind, (status, code) in ERRORS.items():
            if isinstance(exc, kind):
                headers = (
                    {"Retry-After": str(exc.retry_after)}
                    if isinstance(exc, RateLimited)
                    else None
                )
                return _error(request, status, code, str(exc), headers)
        log.error("unhandled webapp error %s", type(exc).__name__)
        return _error(request, 500, "internal_error", "服务器内部错误")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # 只说哪些字段有问题，绝不回显输入值（输入里可能就是密码）。
        """请求格式不对（缺字段、类型错、多了字段、太长）时返回 400，只列出字段名。"""
        fields = sorted(
            {".".join(str(p) for p in e["loc"][1:]) or "body" for e in exc.errors()}
        )
        return _error(
            request, 400, "invalid_request", "请求格式不正确：" + "、".join(fields)
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(request: Request, exc: SQLAlchemyError):
        # 账户库出问题时一律返回“暂时不可用”，不放行（验收 A10）；日志只记错误类型和编号。
        """数据库出错时返回 503；日志只记录错误类型和 SQLSTATE 编号，不记录 SQL 内容。"""
        state = getattr(getattr(exc, "orig", None), "sqlstate", None) or "unknown"
        log.error("account database error %s (SQLSTATE %s)", type(exc).__name__, state)
        return _error(request, 503, "service_unavailable", "服务暂时不可用，请稍后再试")

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        """兜底：任何没预料到的错误都返回 500 和固定提示，不把内部细节暴露给浏览器。"""
        log.error("unexpected error %s", type(exc).__name__)
        return _error(request, 500, "internal_error", "服务器内部错误")

    app.include_router(auth_router)
    app.include_router(password_reset_router)
    return app
