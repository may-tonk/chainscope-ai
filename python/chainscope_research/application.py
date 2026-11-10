"""研究包公开的主 Agent 流入口与分析服务类型；供认证后的调用方使用。

本步没有 HTTP/SSE 路由、任务存储或账户鉴权。事件必须经网站服务验证归属后交付。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import cast

from langsmith import tracing_context

from chainscope_research.agent.main_graph import build_main_graph, model_messages
from chainscope_research.contracts.main_agent import (
    CompletedEvent,
    DecisionEvent,
    DelegateAction,
    ErrorEvent,
    InterruptedEvent,
    MainStreamEvent,
    MainTurnContext,
    MainTurnRequest,
    StartedEvent,
    TextDeltaEvent,
)
from chainscope_research.core.errors import ResearchRuntimeError
from chainscope_research.core.prompt_loader import LoadedPrompt
from chainscope_research.model.factory import MainModel
from chainscope_research.model.fallback import ModelPool
from chainscope_research.services.data_tools import (
    AnalysisToolService as AnalysisApplication,
)

# 研究包的工具公开入口；具体执行仍在 services，网站只需从本模块获取类型。
from chainscope_research.services.turn_control import TurnControl, TurnInterrupted

__all__ = ["AnalysisApplication", "MainAgentApplication"]


class MainAgentApplication:
    """模型和只读提示词可以复用，消息、控制器、输出缓冲与序号均按调用隔离。"""

    def __init__(
        self,
        model: MainModel | ModelPool,
        decision_prompt: LoadedPrompt,
        response_prompt: LoadedPrompt,
        *,
        turn_timeout_seconds: int,
        max_input_characters: int,
        max_output_characters: int = 64000,
    ):
        """输入装配资源及单轮上限；预算是本地限制，不替代未来共享预算服务。"""
        decision_prompt.require_variables(("decision_schema",))
        response_prompt.require_variables(("decision",))
        self.model = model
        self.decision_prompt = decision_prompt
        self.response_prompt = response_prompt
        self.turn_timeout_seconds = turn_timeout_seconds
        self.max_input_characters = max_input_characters
        self.max_output_characters = max_output_characters
        self.prompt_refs = (decision_prompt.reference, response_prompt.reference)

    async def astream_turn(
        self,
        request: MainTurnRequest,
        context: MainTurnContext,
        *,
        control: TurnControl | None = None,
    ) -> AsyncIterator[MainStreamEvent]:
        """逐事件输出本轮回复；调用方可传入控制器停止旧回复，不能据此取消后台研究。

        断开连接导致的协程取消直接传播，不吞成业务成功。持久任务生命周期尚未接入。
        """
        control = control or TurnControl(context)
        if control.context != context:
            raise ValueError("回复控制器不属于当前用户、会话和轮次")
        sequence = 0
        creator = getattr(self.model, "for_turn", None)
        model = creator() if creator is not None else cast(MainModel, self.model)

        def event(payload) -> MainStreamEvent:
            """将内部类型化事件绑定到服务端上下文，生成单轮单调序号。"""
            nonlocal sequence
            sequence += 1
            return MainStreamEvent(
                session_id=context.session_id,
                turn_id=context.turn_id,
                request_revision=context.request_revision,
                sequence=sequence,
                payload=payload,
                model_attempts=tuple(getattr(model, "records", ()))
                if isinstance(payload, (CompletedEvent, ErrorEvent, InterruptedEvent))
                else (),
                prompt_refs=self.prompt_refs
                if isinstance(payload, (CompletedEvent, ErrorEvent, InterruptedEvent))
                else (),
            )

        yield event(StartedEvent())
        try:
            if (
                sum(len(message.content) for message in request.messages)
                > self.max_input_characters
            ):
                raise ResearchRuntimeError(
                    "INPUT_TOO_LARGE", "对话超过本轮输入上限，请缩短内容。"
                )
            # 本步未启用外部观测服务，即使运行机器设置了自动追踪，也不上传私有对话。
            with tracing_context(enabled=False):
                # 超时仅包裹等待模型的操作；不能跨 yield 挂着超时上下文，
                # 否则调用方处理某个事件时，也可能意外被本轮计时器取消。
                deadline = asyncio.get_running_loop().time() + self.turn_timeout_seconds
                control.check()
                graph = build_main_graph(model, self.decision_prompt)
                async with asyncio.timeout(self.turn_timeout_seconds):
                    state = await graph.ainvoke(
                        {"request": request, "control": control}
                    )
                decision = state["decision"]
                control.check()
                yield event(
                    DecisionEvent(
                        decision=decision,
                        dispatch_status="not_connected"
                        if isinstance(decision.action, DelegateAction)
                        else "not_requested",
                    )
                )
                prompt = self.response_prompt.render(
                    decision=decision.model_dump_json()
                )
                text = ""
                async with aclosing(
                    model.stream(model_messages(prompt, request))
                ) as stream:
                    while True:
                        try:
                            remaining = max(
                                0, deadline - asyncio.get_running_loop().time()
                            )
                            async with asyncio.timeout(remaining):
                                piece = await control.run(anext(stream))
                        except StopAsyncIteration:
                            break
                        control.check()
                        if not piece:
                            continue
                        text += piece
                        if len(text) > self.max_output_characters:
                            raise ResearchRuntimeError(
                                "OUTPUT_TOO_LARGE", "回复超过本轮输出上限。"
                            )
                        yield event(TextDeltaEvent(text=piece))
                control.check()
                if not text.strip():
                    raise ResearchRuntimeError(
                        "MODEL_OUTPUT_EMPTY", "主模型未返回有效正文。"
                    )
                yield event(CompletedEvent(text=text))
        except TurnInterrupted as error:
            yield event(InterruptedEvent(reason=error.reason))
        except TimeoutError:
            yield event(
                ErrorEvent(code="TURN_TIMEOUT", message="本轮回复超时，尚未完成。")
            )
        except ResearchRuntimeError as error:
            yield event(ErrorEvent(code=error.code, message=error.public_message))
