"""已登录网页聊天入口：POST＋Cookie/CSRF 认证，响应为主 LLM 的真实 SSE 事件。"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BeforeValidator
from pydantic_core import to_json
from sse_starlette import EventSourceResponse

from chainscope_research.contracts.main_agent import MainTurnRequest
from chainscope_research.core.errors import ResearchRuntimeError

from ..services.sessions import SessionInfo, lookup
from .cookies import COOKIE_NAME
from .dependencies import require_user_csrf, web_context

router = APIRouter(prefix="/api/chat", tags=["chat"])


def parse_chat_json(value) -> MainTurnRequest:
    """按原合同的 JSON 模式校验，兼容 HTTP 数组而不放宽字段、角色或严格类型。"""
    return MainTurnRequest.model_validate_json(to_json(value))


# FastAPI 默认先解析成 Python list；JSON 模式允许数组映射 tuple，避免误拒合法请求。
ChatBody = Annotated[MainTurnRequest, BeforeValidator(parse_chat_json)]


@router.post("/turn")
async def chat_turn(
    request: Request,
    payload: ChatBody,
    session: SessionInfo = Depends(require_user_csrf),
):
    """浏览器只提供对话文本；身份和本轮关联由已认证会话及服务端生成。"""
    service = request.app.state.chat
    user_id = str(session.user_id)
    if len(payload.messages) > service.settings.max_messages:
        return JSONResponse(
            {
                "code": "chat_history_too_large",
                "message": "当前对话过长，请新建对话。",
                "request_id": request.state.request_id,
            },
            status_code=400,
        )
    # 在懒加载及输入检查前占用名额，避免大量并发请求反复装配模型。
    if not service.acquire(user_id):
        return JSONResponse(
            {
                "code": "chat_busy",
                "message": "当前回复较多，请稍后重试。",
                "request_id": request.state.request_id,
            },
            status_code=429,
            headers={"Retry-After": "2"},
        )
    released = False

    def release():
        """正常结束、断开及超时清理共享同一租约，避免提前断开导致名额泄漏。"""
        nonlocal released
        if not released:
            released = True
            service.release(user_id)

    async def disconnected(message):
        """SSE 检测连接关闭时也归还尚未开始迭代的租约；不会重复释放。"""
        release()

    try:
        application = await service.application()
        if (
            sum(len(message.content) for message in payload.messages)
            > application.max_input_characters
        ):
            release()
            return JSONResponse(
                {
                    "code": "chat_history_too_large",
                    "message": "当前对话过长，请新建对话。",
                    "request_id": request.state.request_id,
                },
                status_code=400,
            )
    except ResearchRuntimeError:
        release()
        return JSONResponse(
            {
                "code": "chat_unavailable",
                "message": "聊天服务暂时不可用，请稍后重试。",
                "request_id": request.state.request_id,
            },
            status_code=503,
        )
    except BaseException:
        release()
        raise
    context = web_context(request)
    token = request.cookies.get(COOKIE_NAME)

    async def authorize():
        """长回复发布前周期复核会话；数据库调用在线程完成，不跨流占用连接。"""
        current = await asyncio.to_thread(lookup, context.engine, context.keys, token)
        return (
            current is not None
            and current.kind == "user"
            and current.user_id == session.user_id
        )

    return EventSourceResponse(
        service.events(application, payload, user_id, authorize, release),
        ping=service.settings.heartbeat_seconds,
        send_timeout=service.settings.send_timeout_seconds,
        client_close_handler_callable=disconnected,
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )
