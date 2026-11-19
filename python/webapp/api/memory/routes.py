"""持久聊天 API：创建、分页、恢复、停止和删除用户自己的会话。

这里只负责 HTTP 与身份转换；不允许浏览器指定用户、历史角色或数据库终态。
"""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BeforeValidator, StringConstraints
from pydantic_core import to_json
from sse_starlette import EventSourceResponse

from chainscope_research.contracts.common import Contract, PositiveInt
from chainscope_research.memory.contracts.conversations import SendMessageInput
from chainscope_research.memory.storage.conversations import MemoryStoreError

from ...services.sessions import lookup
from ..cookies import COOKIE_NAME
from ..dependencies import require_user, require_user_csrf, web_context

router = APIRouter(prefix="/api/memory/conversations", tags=["memory"])


class CreateInput(Contract):
    """新会话只允许标题，用户身份来自认证会话。"""

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)] = "新对话"


class RevisionInput(Contract):
    """删除或停止需要明确修订，防止旧页面作用于新轮次。"""

    expected_revision: PositiveInt


def parse_turn(value):
    """保持严格 JSON 合同，同时允许 JSON UUID 字符串进入 UUID 类型。"""
    return SendMessageInput.model_validate_json(to_json(value))


TurnInput = Annotated[SendMessageInput, BeforeValidator(parse_turn)]


@router.post("")
async def create(
    request: Request, payload: CreateInput, session=Depends(require_user_csrf)
):
    """为已登录用户创建持久会话，不执行模型调用。"""
    service = request.app.state.memory_chat
    await service.ready()
    row = await service.db(
        service.store.create_conversation,
        service.context(session.user_id),
        payload.title[: service.settings.history.title_characters],
    )
    return {"conversation": row.model_dump(mode="json")}


@router.get("")
async def list_owned(
    request: Request,
    limit: int | None = None,
    before_time: str | None = None,
    before_id: UUID | None = None,
    session=Depends(require_user),
):
    """分页列出当前用户会话；游标作为值传递，不拼接 SQL。"""
    from datetime import datetime

    service = request.app.state.memory_chat
    await service.ready()
    before = None
    if before_time is not None or before_id is not None:
        if before_time is None or before_id is None:
            raise MemoryStoreError("INVALID_INPUT")
        try:
            before = (datetime.fromisoformat(before_time), before_id)
        except ValueError:
            raise MemoryStoreError("INVALID_INPUT") from None
    try:
        rows = await service.db(
            service.store.list_conversations,
            service.context(session.user_id),
            limit=limit,
            before=before,
        )
    except ValueError:
        raise MemoryStoreError("INVALID_INPUT") from None
    return {"conversations": [row.model_dump(mode="json") for row in rows]}


@router.get("/{conversation_id}")
async def detail(
    conversation_id: UUID,
    request: Request,
    after_sequence: int = 0,
    limit: int | None = None,
    session=Depends(require_user),
):
    """返回已保存消息；过期运行轮次按配置恢复为停止，不继续后台生成。"""
    service = request.app.state.memory_chat
    try:
        return await service.snapshot(
            service.context(session.user_id),
            conversation_id,
            after_sequence=after_sequence,
            limit=limit,
        )
    except ValueError:
        raise MemoryStoreError("INVALID_INPUT") from None


@router.post("/{conversation_id}/delete")
async def delete(
    conversation_id: UUID,
    request: Request,
    payload: RevisionInput,
    session=Depends(require_user_csrf),
):
    """按用户和修订删除会话及原文；当前没有长期记忆关联清理。"""
    service = request.app.state.memory_chat
    await service.ready()
    context = service.context(session.user_id)
    await service.db(
        service.store.delete_conversation,
        context,
        conversation_id,
        expected_revision=payload.expected_revision,
    )
    for (owner, cid, _), control in tuple(service.controls.items()):
        if owner == context.user_id and cid == conversation_id:
            control.interrupt("user_stop")
    return {"deleted": True}


@router.post("/{conversation_id}/turns/{turn_id}/stop")
async def stop(
    conversation_id: UUID,
    turn_id: UUID,
    request: Request,
    payload: RevisionInput,
    session=Depends(require_user_csrf),
):
    """显式停止辅助及时取消；关闭页面的断连收尾仍由 SSE 生成器负责。"""
    service = request.app.state.memory_chat
    await service.ready()
    await service.stop(
        service.context(session.user_id),
        conversation_id,
        turn_id,
        payload.expected_revision,
    )
    return {"stopped": True}


@router.post("/{conversation_id}/turns")
async def generate(
    conversation_id: UUID,
    request: Request,
    payload: TurnInput,
    session=Depends(require_user_csrf),
):
    """接收本轮消息，历史从 PG 装配；SSE 中的每批正文先落库后交付。"""
    service = request.app.state.memory_chat
    if payload.conversation_id != conversation_id:
        raise MemoryStoreError("INVALID_INPUT")
    user_id = str(session.user_id)
    if not service.chat.acquire(user_id):
        raise MemoryStoreError("CHAT_BUSY")
    released = False

    def release():
        """模型流真正结束后归还实例名额，重复收尾不重复释放。"""
        nonlocal released
        if not released:
            released = True
            service.chat.release(user_id)

    context = service.context(session.user_id)
    try:
        application = await service.chat.application()
        accepted, model_input = await service.prepare(context, payload, application)
    except BaseException:
        release()
        raise
    token = request.cookies.get(COOKIE_NAME)
    web = web_context(request)

    async def authorize():
        """沿用现有会话周期复核，身份始终由 Cookie 会话派生。"""
        current = await asyncio.to_thread(lookup, web.engine, web.keys, token)
        return (
            current is not None
            and current.kind == "user"
            and current.user_id == session.user_id
        )

    async def disconnected(message):
        """未开始迭代也尝试收尾；已运行流由生成器 finally 持有额度直到退出。"""
        if (
            accepted.turn.status == "queued"
            and (context.user_id, conversation_id, accepted.turn.turn_id)
            not in service.controls
        ):
            try:
                await service.disconnected(context, accepted)
            finally:
                release()

    return EventSourceResponse(
        service.events(application, model_input, context, accepted, authorize, release),
        ping=service.chat.settings.heartbeat_seconds,
        send_timeout=service.chat.settings.send_timeout_seconds,
        client_close_handler_callable=disconnected,
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )
