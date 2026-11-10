"""主 Agent 单轮决策图：异步调用模型后执行 Pydantic 校验，不注册虚假工具。

正文流由 application 逐块交付，避免把整个回复缓存在图完成之后才发送。
"""

import json
from typing import TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from chainscope_research.contracts.main_agent import MainDecision, MainTurnRequest
from chainscope_research.core.errors import ResearchRuntimeError
from chainscope_research.core.prompt_loader import LoadedPrompt
from chainscope_research.model.factory import MainModel
from chainscope_research.services.turn_control import TurnControl


class MainState(TypedDict, total=False):
    """单次 ainvoke 专属状态，无 checkpoint，无跨用户自动历史继承。"""

    request: MainTurnRequest
    control: TurnControl
    raw_decision: str
    decision: MainDecision


def model_messages(system: str, request: MainTurnRequest) -> list[BaseMessage]:
    """输入服务端系统提示与已校验历史，返回明确分角色消息；不上传可信身份。"""
    return [SystemMessage(content=system)] + [
        HumanMessage(content=message.content)
        if message.role == "user"
        else AIMessage(content=message.content)
        for message in request.messages
    ]


def build_main_graph(model: MainModel, decision_prompt: LoadedPrompt):
    """绑定实际模型与提示词，返回可异步调用的图；不在构造时请求供应商。"""
    system = decision_prompt.render(
        decision_schema=json.dumps(MainDecision.model_json_schema(), ensure_ascii=False)
    )

    async def decide(state: MainState) -> dict:
        """根据当前历史调用模型；控制器允许新消息在供应商等待期间打断旧轮。"""
        raw = await state["control"].run(
            model.decide(model_messages(system, state["request"]))
        )
        return {"raw_decision": raw}

    async def validate(state: MainState) -> dict:
        """解析受限决策；未知角色、额外字段与不完整 JSON 都失败，不自动修正重试。"""
        state["control"].check()
        try:
            decision = MainDecision.model_validate_json(state["raw_decision"])
        except ValidationError:
            raise ResearchRuntimeError(
                "MODEL_DECISION_INVALID", "主模型决策格式无效，本轮未派发任何任务。"
            ) from None
        return {"decision": decision}

    graph = StateGraph(MainState)
    graph.add_node("decide", decide)
    graph.add_node("validate", validate)
    graph.add_edge(START, "decide")
    graph.add_edge("decide", "validate")
    graph.add_edge("validate", END)
    return graph.compile(name="chainscope_main_turn")
