"""网页主 LLM 聊天：懒加载资源、有界并发、会话复核和流断开后的取消清理。

每轮历史由当前页面明确提供，只在该请求内使用；不保存会话记忆或伪造研究任务。
"""

import asyncio
import logging
import time
from collections import Counter
from contextlib import aclosing
from uuid import uuid4

from chainscope_research.bootstrap import build_main_application
from chainscope_research.contracts.main_agent import (
    ErrorEvent,
    MainStreamEvent,
    MainTurnContext,
)
from chainscope_research.services.turn_control import TurnControl

from ..core.settings import ROOT

log = logging.getLogger("webapp.chat")


class ChatService:
    """模型资源按应用复用，消息与控制器按请求隔离；不在模块全局保存用户数据。"""

    def __init__(self, settings, application=None):
        """注入传输设置和可选测试应用；正常运行首次聊天才装配模型。"""
        self.settings = settings
        self._application = application
        self._model_lock = asyncio.Lock()
        self._users = Counter()
        self._total = 0

    async def application(self):
        """首次调用读取现有模型 YAML；失败允许下次重试，不影响其他账户接口启动。"""
        async with self._model_lock:
            if self._application is None:
                self._application = await asyncio.to_thread(
                    build_main_application, ROOT
                )
        return self._application

    def acquire(self, user_id: str) -> bool:
        """事件循环内原子检查实例及用户上限；不排无界等待队列。"""
        if (
            self._total >= self.settings.max_parallel_turns
            or self._users[user_id] >= self.settings.max_turns_per_user
        ):
            return False
        self._total += 1
        self._users[user_id] += 1
        return True

    def release(self, user_id: str) -> None:
        """真实流生成器结束后归还名额；移除已结束用户，避免长期积累标识。"""
        self._total -= 1
        self._users[user_id] -= 1
        if self._users[user_id] == 0:
            del self._users[user_id]

    async def events(self, application, payload, user_id, authorize, release):
        """输出 SSE 数据；断开时传播取消并关闭上游，不把已发送部分标成完成。"""
        context = MainTurnContext(
            user_id=user_id,
            session_id=str(uuid4()),
            turn_id=str(uuid4()),
            request_revision=1,
        )
        control = TurnControl(context)
        sequence = 0
        checked_at = time.monotonic()
        try:
            async with aclosing(
                application.astream_turn(payload, context, control=control)
            ) as stream:
                async for event in stream:
                    if time.monotonic() - checked_at >= self.settings.reauth_seconds:
                        if not await authorize():
                            yield {
                                "data": MainStreamEvent(
                                    session_id=context.session_id,
                                    turn_id=context.turn_id,
                                    request_revision=1,
                                    sequence=sequence + 1,
                                    payload=ErrorEvent(
                                        code="not_authenticated",
                                        message="登录已失效，请重新登录。",
                                    ),
                                ).model_dump_json()
                            }
                            return
                        checked_at = time.monotonic()
                    sequence = event.sequence
                    yield {"data": event.model_dump_json()}
        except asyncio.CancelledError:
            control.interrupt("user_stop")
            raise
        except Exception as error:  # noqa: BLE001 -- SSE 已开始后不能返回 JSON/HTML 异常页。
            log.error("chat stream failed type=%s", type(error).__name__)
            yield {
                "data": MainStreamEvent(
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    request_revision=1,
                    sequence=sequence + 1,
                    payload=ErrorEvent(
                        code="CHAT_STREAM_FAILED", message="本轮回复未能完成，请重试。"
                    ),
                ).model_dump_json()
            }
        finally:
            control.interrupt("user_stop")
            release()
