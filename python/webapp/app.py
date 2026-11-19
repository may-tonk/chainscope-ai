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

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from chainscope_research.memory.settings import load_memory_settings
from chainscope_research.memory.storage.conversations import (
    ConversationStore,
    MemoryStoreError,
)
from chainscope_research.memory.storage.database import engine_for as memory_engine_for
from chainscope_research.memory.storage.database import require_identity

from .api.dependencies import WebContext
from .api.memory.routes import router as memory_router
from .api.routes_auth import router as auth_router
from .api.routes_chat import router as chat_router
from .api.routes_password_reset import router as password_reset_router
from .core.app_config import AppConfig, load_app_config
from .core.chat_settings import load_chat_settings
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
from .services.chat import ChatService
from .services.memory.chat import PersistentChatService

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
    chat_application=None,
    memory_store=None,
    memory_settings=None,
) -> FastAPI:
    """创建网站后端。参数都不传时从 .env 读取；测试时传入测试库、临时密钥等。"""
    config = config or load_app_config()
    keys = keys or load_keys()
    owns_engine = engine is None
    profile = None
    if engine is None:
        if config.is_production:
            # 生产环境的数据库配置在部署阶段增加，现在不允许用开发库冒充。
            raise ConfigError("生产环境的账户库配置尚未实现")
        profile = load_profile("development")
        engine = engine_for(profile)
    mailer = mailer or mailer_for(config)

    @asynccontextmanager
    async def lifespan(application):
        """退出时等待持久写入并释放本工厂拥有的池；注入测试池由调用方管理。"""
        try:
            yield
        finally:
            await application.state.memory_chat.close()
            if owns_engine:
                if memory_engine is not None:
                    await asyncio.to_thread(memory_engine.dispose)
                await asyncio.to_thread(engine.dispose)

    app = FastAPI(
        title="ChainScope Web API",
        docs_url="/docs" if config.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.webapp = WebContext(engine, keys, config, mailer)
    # 独立聊天资源不读取用户历史、不运行研究工具，也不在启动时调用模型。
    app.state.chat = ChatService(load_chat_settings(), chat_application)
    settings = memory_settings or load_memory_settings()
    memory_engine = None
    initializer = None
    if memory_store is None and hasattr(engine, "dialect"):
        memory_engine = (
            memory_engine_for(profile, settings=settings) if owns_engine else engine
        )
        memory_store = ConversationStore(memory_engine, settings=settings)
        if profile is not None:

            def initialize_memory():
                """首次使用只检查身份和版本，不自动创建或升级结构。"""
                from sqlalchemy import text

                with memory_engine.connect() as connection:
                    require_identity(connection, profile)
                    if (
                        connection.scalar(
                            text(
                                "SELECT version_num FROM research.memory_alembic_version"
                            )
                        )
                        != "mem0001"
                    ):
                        raise MemoryStoreError("DATABASE_FAILURE")

            initializer = initialize_memory
    app.state.memory_chat = PersistentChatService(
        memory_store, settings, app.state.chat, initializer
    )
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

    @app.exception_handler(MemoryStoreError)
    async def memory_error(request: Request, exc: MemoryStoreError):
        """Memory 错误只映射稳定代码，资源不存在与不属于当前用户使用相同响应。"""
        status, code, message = {
            "NOT_FOUND": (404, "conversation_not_found", "会话不存在或不可访问。"),
            "REVISION_CONFLICT": (
                409,
                "conversation_changed",
                "会话已更新，请刷新后重试。",
            ),
            "IDEMPOTENCY_CONFLICT": (
                409,
                "request_conflict",
                "请求编号已被不同内容使用。",
            ),
            "TURN_ALREADY_RUNNING": (
                409,
                "turn_running",
                "该轮回复正在生成，请查看历史。",
            ),
            "CHAT_BUSY": (429, "chat_busy", "当前回复较多，请稍后重试。"),
            "HISTORY_TOO_LARGE": (
                400,
                "chat_history_too_large",
                "当前会话超过上下文上限，请新建对话。",
            ),
            "INVALID_INPUT": (400, "invalid_request", "请求参数不正确。"),
            "STALE_TURN": (409, "turn_changed", "该轮回复已结束或被新问题取代。"),
        }.get(exc.code, (503, "memory_unavailable", "对话存储暂时不可用，请稍后重试。"))
        return _error(request, status, code, message)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        """兜底：任何没预料到的错误都返回 500 和固定提示，不把内部细节暴露给浏览器。"""
        log.error("unexpected error %s", type(exc).__name__)
        return _error(request, 500, "internal_error", "服务器内部错误")

    app.include_router(auth_router)
    app.include_router(password_reset_router)
    app.include_router(chat_router)
    app.include_router(memory_router)
    return app
