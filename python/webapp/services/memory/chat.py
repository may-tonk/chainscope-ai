"""持久聊天接线：受控数据库线程、服务端历史、分批提交后交付 SSE。

复用主 Agent 与既有聊天并发限制，运行参数由 Memory YAML 注入。
实例只保存活动取消控制器；历史和终态以 PostgreSQL 为准。
此文件是网站流传输适配，集中归入 services/memory，不实现长期记忆提取或裁决。
"""

import asyncio
import logging
import time
from contextlib import suppress
from uuid import UUID

from chainscope_research.contracts.main_agent import (
    ConversationMessage,
    ErrorEvent,
    InterruptedEvent,
    MainStreamEvent,
    MainTurnContext,
    MainTurnRequest,
    StartedEvent,
    TextDeltaEvent,
)
from chainscope_research.memory.contracts.common import MemoryUserContext
from chainscope_research.memory.storage.conversations import MemoryStoreError

log = logging.getLogger("webapp.memory_chat")


class PersistentChatService:
    """装配已验证存储、配置与主聊天资源；构造不调用模型或执行迁移。"""

    def __init__(self, store, settings, chat, initializer=None):
        """保留依赖，数据库线程有界；initializer 在首次使用时验证结构和身份。"""
        self.store, self.settings, self.chat = store, settings, chat
        self._initializer = initializer
        self._ready = False
        self._ready_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(settings.stream.database_parallelism)
        self._writes = set()
        self.controls = {}

    async def db(self, function, *args, **kwargs):
        """在线程执行同步 SQL；调用方取消后线程槽位仍保持到操作实际结束。"""

        async def run():
            """独立任务持有槽位，避免外层取消提前释放真实数据库并发额度。"""
            async with self._slots:
                return await asyncio.to_thread(function, *args, **kwargs)

        task = asyncio.create_task(run())
        self._writes.add(task)

        # 即使请求取消，也消费后台异常，避免异常正文被事件循环默认日志输出。
        def finished(done):
            """归还任务引用并读取安全异常；业务调用方仍能通过 await 收到异常。"""
            self._writes.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def ready(self):
        """首次访问检查存储及迁移前提，失败不退回内存聊天或自动建表。"""
        if self.store is None:
            raise MemoryStoreError("DATABASE_FAILURE")
        async with self._ready_lock:
            if not self._ready:
                if self._initializer is not None:
                    await self.db(self._initializer)
                self._ready = True

    async def close(self):
        """应用退出时打断本实例生成，并有界等待已经发出的数据库操作。"""
        for control in tuple(self.controls.values()):
            control.interrupt("user_stop")
        if self._writes:
            pending = asyncio.gather(*tuple(self._writes), return_exceptions=True)
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(pending),
                    self.settings.stream.cleanup_timeout_seconds,
                )

    def context(self, user_id):
        """从已认证会话构造范围；长期记忆代次表尚未接入，此阶段只操作会话。"""
        return MemoryUserContext(user_id=UUID(str(user_id)), memory_generation=0)

    async def snapshot(self, context, conversation_id, *, after_sequence=0, limit=None):
        """读取前恢复过期轮次，返回分页快照；当前实例运行态也来自数据库。"""
        await self.ready()
        await self.db(self.store.recover_stale_turns, context, conversation_id)
        conversation = await self.db(
            self.store.get_conversation, context, conversation_id
        )
        size = self.settings.history.page_size if limit is None else limit
        rows = await self.db(
            self.store.list_messages,
            context,
            conversation_id,
            after_sequence=after_sequence,
            limit=size,
        )
        return {
            "conversation": conversation.model_dump(mode="json"),
            "messages": [row.model_dump(mode="json") for row in rows],
            "next_sequence": rows[-1].sequence if rows else after_sequence,
            "has_more": len(rows) == size,
            "poll_after_ms": self.settings.history.poll_seconds * 1000,
        }

    async def prepare(self, context, request, application):
        """从 PG 读取有效历史并检查预算，再原子接收本轮；不信任浏览器历史。"""
        await self.ready()
        await self.db(self.store.recover_stale_turns, context, request.conversation_id)
        existing = await self.db(self.store.find_request, context, request)
        if existing is not None:
            if existing.turn.status == "running":
                raise MemoryStoreError("TURN_ALREADY_RUNNING")
            if existing.turn.status != "queued":
                return existing, MainTurnRequest(
                    messages=(
                        ConversationMessage(role="user", content=request.content),
                    )
                )
        history = await self.db(
            self.store.list_messages,
            context,
            request.conversation_id,
            limit=self.settings.history.max_page_size,
        )
        if (
            len(history) + (0 if existing is not None else 2)
            > self.settings.history.max_context_messages
        ):
            raise MemoryStoreError("HISTORY_TOO_LARGE")
        texts = [
            row.content
            for row in history
            if row.role == "user" or row.status == "completed"
        ]
        if (
            sum(map(len, texts)) + (0 if existing is not None else len(request.content))
            > application.max_input_characters
        ):
            raise MemoryStoreError("HISTORY_TOO_LARGE")
        accepted = await self.db(self.store.accept_message, context, request)
        # 同实例及时取消旧模型等待；跨实例通过修订号与心跳拒绝旧写回。
        for (owner, conversation_id, turn_id), control in tuple(self.controls.items()):
            if (
                owner == context.user_id
                and conversation_id == request.conversation_id
                and turn_id != accepted.turn.turn_id
            ):
                control.interrupt("superseded")
        if accepted.replayed and accepted.turn.status == "running":
            raise MemoryStoreError("TURN_ALREADY_RUNNING")
        if accepted.replayed and accepted.turn.status != "queued":
            return accepted, MainTurnRequest(
                messages=(ConversationMessage(role="user", content=request.content),)
            )
        rows = await self.db(
            self.store.list_messages,
            context,
            request.conversation_id,
            limit=self.settings.history.max_page_size,
        )
        prompt = tuple(
            ConversationMessage(role=row.role, content=row.content)
            for row in rows
            if row.content.strip() and (row.role == "user" or row.status == "completed")
        )
        return accepted, MainTurnRequest(messages=prompt)

    async def stop(self, context, conversation_id, turn_id, revision):
        """确认资源归属后停止；控制器唤醒本实例模型等待，DB 终态封锁迟到写入。"""
        turn = await self.db(self.store.get_turn, context, conversation_id, turn_id)
        if turn.request_revision != revision:
            raise MemoryStoreError("STALE_TURN")
        control = self.controls.get((context.user_id, conversation_id, turn_id))
        if control is not None:
            control.interrupt("user_stop")
        if turn.status in {"queued", "running"}:
            await self.db(
                self.store.finish_turn,
                context,
                conversation_id,
                turn_id,
                revision,
                status="stopped",
                stop_reason="user_stop",
            )

    async def disconnected(self, context, accepted):
        """流尚未迭代就断连时也将排队轮收尾，避免永久留下 queued。"""
        if not accepted.replayed:
            with suppress(MemoryStoreError):
                await self.db(
                    self.store.finish_turn,
                    context,
                    accepted.turn.conversation_id,
                    accepted.turn.turn_id,
                    accepted.turn.request_revision,
                    status="stopped",
                    stop_reason="connection_lost",
                )

    async def events(self, application, request, context, accepted, authorize, release):
        """输出重新编号的持久 SSE；正文每次先提交 PG，再向浏览器发布。"""
        from chainscope_research.contracts.main_agent import CompletedEvent
        from chainscope_research.services.turn_control import TurnControl

        turn = accepted.turn
        main_context = MainTurnContext(
            user_id=str(context.user_id),
            session_id=str(turn.conversation_id),
            turn_id=str(turn.turn_id),
            request_revision=turn.request_revision,
        )
        control = TurnControl(main_context)
        key = (context.user_id, turn.conversation_id, turn.turn_id)
        args = (context, turn.conversation_id, turn.turn_id, turn.request_revision)
        sequence, offset, pending = 0, 0, ""
        committed = ""
        claimed, terminal, next_task = False, False, None
        last_heartbeat = last_auth = last_flush = time.monotonic()

        def event(payload, original=None):
            """批次合并后重新分配连续序号，保留原模型终态的审计信息。"""
            nonlocal sequence
            sequence += 1
            if original is not None:
                return {
                    "data": original.model_copy(
                        update={"sequence": sequence, "payload": payload}
                    ).model_dump_json()
                }
            return {
                "data": MainStreamEvent(
                    session_id=main_context.session_id,
                    turn_id=main_context.turn_id,
                    request_revision=turn.request_revision,
                    sequence=sequence,
                    payload=payload,
                ).model_dump_json()
            }

        async def flush():
            """提交待发正文，成功后返回一个 SSE 帧；重复偏移写入由存储幂等处理。"""
            nonlocal pending, offset, last_flush, committed
            text = pending
            await self.db(
                self.store.append_reply, *args, expected_length=offset, text=text
            )
            offset += len(text)
            committed += text
            pending = ""
            last_flush = time.monotonic()
            return event(TextDeltaEvent(text=text))

        try:
            if accepted.replayed and turn.status in {"completed", "stopped", "failed"}:
                yield event(StartedEvent())
                if accepted.assistant_message.content:
                    yield event(TextDeltaEvent(text=accepted.assistant_message.content))
                if turn.status == "completed":
                    yield event(CompletedEvent(text=accepted.assistant_message.content))
                elif turn.status == "stopped":
                    yield event(InterruptedEvent(reason="user_stop"))
                else:
                    yield event(
                        ErrorEvent(
                            code=turn.error_code or "CHAT_FAILED",
                            message="该轮回复未完成，可重新提问。",
                        )
                    )
                return
            await self.db(self.store.mark_running, *args, claim=True)
            claimed = True
            self.controls[key] = control
            stream = application.astream_turn(request, main_context, control=control)
            try:
                while True:
                    if next_task is None:
                        next_task = asyncio.create_task(anext(stream))
                    now = time.monotonic()
                    timeout = min(
                        max(
                            0,
                            self.settings.recovery.heartbeat_seconds
                            - (now - last_heartbeat),
                        ),
                        max(0, self.chat.settings.reauth_seconds - (now - last_auth)),
                        max(
                            0,
                            self.settings.stream.flush_interval_seconds
                            - (now - last_flush),
                        )
                        if pending
                        else self.settings.recovery.heartbeat_seconds,
                    )
                    done, _ = await asyncio.wait({next_task}, timeout=timeout)
                    now = time.monotonic()
                    if now - last_auth >= self.chat.settings.reauth_seconds:
                        if not await authorize():
                            raise MemoryStoreError("AUTH_EXPIRED")
                        last_auth = now
                    if now - last_heartbeat >= self.settings.recovery.heartbeat_seconds:
                        await self.db(self.store.heartbeat_turn, *args)
                        last_heartbeat = now
                    if done:
                        try:
                            incoming = next_task.result()
                        except StopAsyncIteration:
                            next_task = None
                            break
                        next_task = None
                        payload = incoming.payload
                        if isinstance(payload, TextDeltaEvent):
                            if not pending:
                                last_flush = now
                            pending += payload.text
                        else:
                            if pending:
                                yield await flush()
                            if isinstance(payload, CompletedEvent):
                                if payload.text != committed:
                                    raise MemoryStoreError("CONTENT_MISMATCH")
                                await self.db(
                                    self.store.finish_turn, *args, status="completed"
                                )
                                terminal = True
                            elif isinstance(payload, ErrorEvent):
                                await self.db(
                                    self.store.finish_turn,
                                    *args,
                                    status="failed",
                                    error_code="MODEL_FAILED",
                                )
                                terminal = True
                            elif isinstance(payload, InterruptedEvent):
                                await self.db(
                                    self.store.finish_turn,
                                    *args,
                                    status="stopped",
                                    stop_reason=payload.reason,
                                )
                                terminal = True
                            yield event(payload, incoming)
                    if pending and (
                        len(pending) >= self.settings.stream.flush_characters
                        or time.monotonic() - last_flush
                        >= self.settings.stream.flush_interval_seconds
                    ):
                        yield await flush()
            finally:
                if next_task is not None:
                    next_task.cancel()
                    await asyncio.gather(next_task, return_exceptions=True)
                    next_task = None
                await stream.aclose()
        except asyncio.CancelledError:
            control.interrupt("user_stop")
            raise
        except Exception as error:  # noqa: BLE001 -- SSE 只输出安全错误，不回显驱动/模型原文。
            log.error("persistent chat failed type=%s", type(error).__name__)
            yield event(
                ErrorEvent(
                    code="not_authenticated"
                    if isinstance(error, MemoryStoreError)
                    and error.code == "AUTH_EXPIRED"
                    else "CHAT_STREAM_FAILED",
                    message="登录已失效，请重新登录。"
                    if isinstance(error, MemoryStoreError)
                    and error.code == "AUTH_EXPIRED"
                    else "本轮回复中断，已保存的内容可在历史中查看。",
                )
            )
        finally:
            control.interrupt("user_stop")
            if next_task is not None:
                next_task.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await next_task
            if self.controls.get(key) is control:
                self.controls.pop(key, None)
            if claimed and not terminal:

                async def cleanup():
                    """取消后的有界收尾；先尽量保存已取得正文，再写停止终态。"""
                    with suppress(MemoryStoreError):
                        if pending:
                            await flush()
                        await self.db(
                            self.store.finish_turn,
                            *args,
                            status="stopped",
                            stop_reason="connection_lost",
                        )

                task = asyncio.create_task(cleanup())
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        self.settings.stream.cleanup_timeout_seconds,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    task.add_done_callback(
                        lambda done: None if done.cancelled() else done.exception()
                    )
                except Exception as error:  # noqa: BLE001 -- 收尾失败仍归还名额，过期恢复继续兜底。
                    log.error("persistent cleanup failed type=%s", type(error).__name__)
            release()
