"""主 Agent 的对话、分派决策和流事件合同；网站认证接入尚未实现。

模型只产生业务决策，用户与会话标识由调用方的可信上下文提供。
"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from chainscope_research.contracts.common import Contract, NonEmptyText, PositiveInt


class ConversationMessage(Contract):
    """只允许用户和助手历史；外部输入不能新增 system 或 tool 消息角色。"""

    role: Literal["user", "assistant"]
    content: NonEmptyText


class MainTurnRequest(Contract):
    """一次主 Agent 请求，历史由未来会话服务按归属读取，不由模型指定用户。"""

    messages: Annotated[tuple[ConversationMessage, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def check_last_message(self) -> Self:
        """输入历史，返回以用户消息结束的请求，避免重复续写助手自己的回答。"""
        if self.messages[-1].role != "user":
            raise ValueError("最后一条消息必须来自用户")
        return self


class MainTurnContext(Contract):
    """服务端关联信息；本数据类不提供认证能力，也不发送给模型。"""

    user_id: NonEmptyText
    session_id: NonEmptyText
    turn_id: NonEmptyText
    request_revision: PositiveInt


class ReplyAction(Contract):
    """一般知识或对话回答；没有工具证据时不得宣称已知真实链上数据。"""

    kind: Literal["reply"]
    response_goal: NonEmptyText


class ClarifyAction(Contract):
    """必要追问；时间追问事件可供未来前端渲染表单，当前尚未接入该表单。"""

    kind: Literal["clarify"]
    questions: Annotated[tuple[NonEmptyText, ...], Field(min_length=1, max_length=3)]
    input_kind: Literal["time", "object", "goal", "other"]


class DelegationTask(Contract):
    """表达所需专业任务，不伪装为已经启动的 subagent 或已经完成的分析。"""

    agent: Literal["information_extraction", "data_analysis", "summary"]
    objective: NonEmptyText


class DelegateAction(Contract):
    """本步仅输出分派请求；后续授权调度服务负责依赖、并行和实际执行。"""

    kind: Literal["delegate"]
    tasks: Annotated[tuple[DelegationTask, ...], Field(min_length=1, max_length=6)]


class MainDecision(Contract):
    """由同源 Pydantic schema 校验的模型决策，不包含权限、身份和任意代码。"""

    action: Annotated[
        ReplyAction | ClarifyAction | DelegateAction, Field(discriminator="kind")
    ]


class StartedEvent(Contract):
    """服务端已开始本轮处理，不代表研究后台任务已经被接受。"""

    type: Literal["turn.started"] = "turn.started"


class DecisionEvent(Contract):
    """已校验的业务事件，面向调用端；不能作为正文 token 直接追加到消息。"""

    type: Literal["decision.ready"] = "decision.ready"
    decision: MainDecision
    dispatch_status: Literal["not_requested", "not_connected"]


class TextDeltaEvent(Contract):
    """仅有用户可见正文的增量，不携带 reasoning_content 或原始响应对象。"""

    type: Literal["message.delta"] = "message.delta"
    text: Annotated[str, Field(min_length=1)]


class CompletedEvent(Contract):
    """仅标记本轮回复结束；delegate 决策不代表研究任务完成。"""

    type: Literal["turn.completed"] = "turn.completed"
    text: NonEmptyText


class ErrorEvent(Contract):
    """失败为终止事件；已发送的部分正文不能被标记为成功完成。"""

    type: Literal["turn.failed"] = "turn.failed"
    code: NonEmptyText
    message: NonEmptyText


class InterruptedEvent(Contract):
    """旧回复被新消息取代，或用户停止当前回复；不代表后台研究已停止。"""

    type: Literal["turn.interrupted"] = "turn.interrupted"
    reason: Literal["superseded", "user_stop"]


class MainStreamEvent(Contract):
    """单轮有序流信封；序号只能在本轮解释，不宣称支持持久事件回放。"""

    schema_version: Literal["1.0"] = "1.0"
    session_id: NonEmptyText
    turn_id: NonEmptyText
    request_revision: PositiveInt
    sequence: PositiveInt
    # 终止时附带安全审计，不含密钥、原始异常和供应商返回正文。
    model_attempts: tuple["ModelAttempt", ...] = ()
    # 仅终止事件附加实际加载的模板标识；不返回完整正文或内部文件路径。
    prompt_refs: tuple["PromptReference", ...] = ()
    payload: Annotated[
        StartedEvent
        | DecisionEvent
        | TextDeltaEvent
        | CompletedEvent
        | ErrorEvent
        | InterruptedEvent,
        Field(discriminator="type"),
    ]


class ModelAttempt(Contract):
    """记录实际模型、阶段与结果；不伪造 token 用量或费用。"""

    stage: Literal["decision", "response"]
    model: NonEmptyText
    outcome: Literal["success", "failed", "interrupted"]
    error_code: NonEmptyText | None = None


class PromptReference(Contract):
    """记录本轮使用的模板版本与内容摘要；摘要是核对依据，不验证提示词质量。"""

    name: NonEmptyText
    version: NonEmptyText
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
