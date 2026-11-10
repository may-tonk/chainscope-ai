"""主 LLM 异步故障切换：每轮独立计数，正文发出后不拼接其他模型。

不使用 SDK/图节点自动重试，不重跑 Agent 或工具；取消不会触发备用调用。
"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from typing import Protocol

from langchain_core.messages import BaseMessage
from pydantic import ValidationError

from chainscope_research.contracts.main_agent import MainDecision, ModelAttempt
from chainscope_research.core.config_schema import RuntimeSettings
from chainscope_research.core.errors import ResearchRuntimeError
from chainscope_research.model.factory import MainModel, ModelCallError


class ModelPool(Protocol):
    """共享无会话状态的模型资源，每次调用创建独立预算对象。"""

    def for_turn(self) -> MainModel:
        """返回本轮专属调用器；禁止跨用户共享可变计数。"""
        ...


class ResilientModels:
    """主备客户端可以复用，但预算和审计记录不复用。"""

    def __init__(
        self, models: tuple[tuple[str, MainModel], ...], runtime: RuntimeSettings
    ):
        """输入已装配、已登记模型，保存不可变候选序列及运行限制。"""
        self.models = models
        self.runtime = runtime

    def for_turn(self) -> "ModelTurn":
        """创建单轮执行对象，不新增权限、记忆或任务。"""
        return ModelTurn(self.models, self.runtime)


class ModelTurn:
    """决策与正文共享单轮尝试预算，每阶段按主、备顺序最多各一次。"""

    def __init__(
        self, models: tuple[tuple[str, MainModel], ...], runtime: RuntimeSettings
    ):
        """为当前轮创建独立尝试记录，模型对象中不保存用户消息。"""
        self.models = models[: runtime.max_attempts_per_stage]
        self.runtime = runtime
        self.records: list[ModelAttempt] = []

    def begin(self, stage, name: str) -> int:
        """调用前占用次数，未发出请求的预算拒绝不计为一次供应商调用。"""
        # 即使未来调用方误重复进入某个阶段，也不能重置该阶段的预算。
        if (
            sum(record.stage == stage for record in self.records)
            >= self.runtime.max_attempts_per_stage
        ):
            raise ResearchRuntimeError(
                "MODEL_ATTEMPTS_EXHAUSTED", "当前阶段模型尝试次数已达上限。"
            )
        if len(self.records) >= self.runtime.max_attempts_per_turn:
            raise ResearchRuntimeError(
                "MODEL_ATTEMPTS_EXHAUSTED", "本轮模型尝试次数已达上限。"
            )
        # 默认记为中断：等待被取消或源流被关闭时，不能误记成成功。
        self.records.append(
            ModelAttempt(stage=stage, model=name, outcome="interrupted")
        )
        return len(self.records) - 1

    def finish(self, index: int, outcome, code: str | None = None) -> None:
        """保存安全的尝试结果；默认中断值覆盖外部取消和源流关闭路径。"""
        previous = self.records[index]
        self.records[index] = ModelAttempt(
            stage=previous.stage, model=previous.model, outcome=outcome, error_code=code
        )

    async def decide(self, messages: Sequence[BaseMessage]) -> str:
        """在结构校验通过后才采用决策；失败只在明确可恢复分类下换备用。"""
        for name, model in self.models:
            index = self.begin("decision", name)
            try:
                raw = await model.decide(messages)
                try:
                    # 校验属于切换边界：JSON 合法但字段错误也可有限尝试备用。
                    MainDecision.model_validate_json(raw)
                except ValidationError:
                    raise ModelCallError(
                        "MODEL_DECISION_INVALID", "模型决策不符合结构合同。", True
                    ) from None
                self.finish(index, "success")
                return raw
            except ModelCallError as error:
                self.finish(index, "failed", error.code)
                if not error.recoverable:
                    raise
            except ResearchRuntimeError as error:
                self.finish(index, "failed", error.code)
                raise
        raise ResearchRuntimeError("MODELS_EXHAUSTED", "主备模型均未返回有效决策。")

    async def stream(
        self, messages: Sequence[BaseMessage]
    ) -> AsyncGenerator[str, None]:
        """只在首个正文增量前切换；部分正文后任何故障都结束，不拼接备用答案。"""
        for name, model in self.models:
            index = self.begin("response", name)
            emitted = False
            leading = ""
            try:
                async with aclosing(model.stream(messages)) as source:
                    async for piece in source:
                        if not piece:
                            continue
                        # JSON/正文服务可能只发空白。首段有效正文前先缓冲，
                        # 既保留 Markdown 缩进，也避免空白响应被误判为已发布答案。
                        if not emitted:
                            leading += piece
                            if len(leading) > self.runtime.max_output_characters:
                                raise ResearchRuntimeError(
                                    "OUTPUT_TOO_LARGE", "回复超过本轮输出上限。"
                                )
                            if not leading.strip():
                                continue
                            piece, leading = leading, ""
                        # 从第一次交付正文开始，后续故障就不可透明拼接备用答案。
                        emitted = True
                        yield piece
                if not emitted:
                    raise ModelCallError(
                        "MODEL_OUTPUT_EMPTY", "模型没有返回正文。", True
                    )
                self.finish(index, "success")
                return
            except ModelCallError as error:
                self.finish(index, "failed", error.code)
                if emitted or not error.recoverable:
                    raise
            except ResearchRuntimeError as error:
                self.finish(index, "failed", error.code)
                raise
        raise ResearchRuntimeError("MODELS_EXHAUSTED", "主备模型均未返回有效正文。")
