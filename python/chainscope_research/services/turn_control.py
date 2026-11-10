"""单轮模型回复的协作式打断；不充当后台研究暂停器或分布式任务协调器。

未来会话服务接收新消息后，按判断调用旧轮 control.interrupt。浏览器断开与
研究任务的停止策略由后台服务分别处理，不能复用这个短生命周期对象保存任务。
"""

import asyncio
from collections.abc import Coroutine
from typing import Any, Literal, TypeVar

from chainscope_research.contracts.main_agent import MainTurnContext

Value = TypeVar("Value")
InterruptReason = Literal["superseded", "user_stop"]


class TurnInterrupted(Exception):
    """明确的回复中断信号，与网络失败和整个研究取消区别处理。"""

    def __init__(self, reason: InterruptReason):
        """输入打断原因，保留给事件层，不包装成普通可重试故障。"""
        super().__init__(reason)
        self.reason = reason


class TurnControl:
    """调用方持有的本轮控制器；没有跨请求可变全局状态，不宣称跨 worker 生效。"""

    def __init__(self, context: MainTurnContext):
        """绑定可信调用上下文；每轮独立创建，防止误把其他用户的控制器传进来。"""
        self.context = context
        self._interrupted = asyncio.Event()
        self._reason: InterruptReason = "user_stop"

    def interrupt(self, reason: InterruptReason) -> None:
        """由持有控制器的会话服务发信号；保留首次原因，不直接操作数据库任务。"""
        if not self._interrupted.is_set():
            self._reason = reason
            self._interrupted.set()

    def check(self) -> None:
        """在发送增量或完成事件之前检查，防止已经作废的回复继续发布。"""
        if self._interrupted.is_set():
            raise TurnInterrupted(self._reason)

    async def run(self, operation: Coroutine[Any, Any, Value]) -> Value:
        """等待模型操作或打断信号；打断优先，取消本地等待并回收子任务。

        取消客户端等待不保证供应商已撤回计算或计费，不能据此承诺费用归零。
        """
        if self._interrupted.is_set():
            operation.close()
            self.check()
        task = asyncio.create_task(operation)
        stopped = asyncio.create_task(self._interrupted.wait())
        try:
            await asyncio.wait((task, stopped), return_when=asyncio.FIRST_COMPLETED)
            self.check()
            return await task
        finally:
            for pending in (task, stopped):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, stopped, return_exceptions=True)
