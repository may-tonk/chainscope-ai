"""主 LLM 的 DeepSeek 异步适配；决策用 JSON Output，正文用原生 token 流。

两次调用各有用途：先取得短决策，再流式组织面向用户的回复。
本适配器不自行重试；主备策略、预算和切换边界统一由 fallback.py 管理。
"""

from collections.abc import AsyncGenerator, Sequence
from typing import Protocol

from langchain_core.messages import BaseMessage
from langchain_deepseek import ChatDeepSeek
from openai import APIConnectionError, APIError
from pydantic import SecretStr

from chainscope_research.core.config_schema import GenerationSettings, ModelProfile
from chainscope_research.core.errors import ResearchRuntimeError


class ModelCallError(ResearchRuntimeError):
    """模型错误附带可恢复分类；仅该分类允许进入预先配置的备用模型。"""

    def __init__(self, code: str, message: str, recoverable: bool):
        """输入安全消息和恢复标记，不保留供应商原始异常正文。"""
        super().__init__(code, message)
        self.recoverable = recoverable


def classify_provider_error(error: APIError) -> ModelCallError:
    """根据连接错误或 HTTP 状态分类；429、凭证及参数错误不盲目换同账号模型。"""
    status = getattr(error, "status_code", None)
    recoverable = isinstance(error, APIConnectionError) or status in {
        500,
        502,
        503,
        504,
    }
    code = {
        400: "MODEL_REQUEST_INVALID",
        401: "MODEL_AUTH_FAILED",
        402: "MODEL_BALANCE_EXHAUSTED",
        403: "MODEL_PERMISSION_DENIED",
        422: "MODEL_REQUEST_INVALID",
        429: "MODEL_RATE_LIMITED",
    }.get(status if isinstance(status, int) else 0, "MODEL_UNAVAILABLE")
    return ModelCallError(
        code, "模型请求未完成，请稍后重试或检查服务配置。", recoverable
    )


def build_client(
    profile: ModelProfile, generation: GenerationSettings, key: SecretStr
) -> ChatDeepSeek:
    """只传已经校验且实际支持的字段；不同阶段分别创建客户端，不混用采样参数。"""
    return ChatDeepSeek(
        model=profile.model,
        api_key=key,
        base_url=profile.base_url,
        timeout=profile.request_timeout_seconds,
        max_retries=profile.sdk_max_retries,
        max_tokens=generation.max_tokens,
        extra_body={"thinking": {"type": generation.thinking}},
        temperature=generation.temperature,
        top_p=generation.top_p,
    )


class MainModel(Protocol):
    """小型模型接口，离线测试可以注入替身，无需伪造数据库或实际工具。"""

    async def decide(self, messages: Sequence[BaseMessage]) -> str:
        """输入系统约束与对话，返回待 Pydantic 校验的 JSON 文本。"""
        ...

    def stream(self, messages: Sequence[BaseMessage]) -> AsyncGenerator[str, None]:
        """输入回复上下文，返回仅包含正文的异步增量流。"""
        ...


class DeepSeekMainModel:
    """适配实际 ChatDeepSeek；不绑定未实现工具，追踪策略由应用入口控制。"""

    def __init__(self, profile: ModelProfile, key: SecretStr):
        """输入已校验配置，创建客户端；构造阶段不发网络请求。"""
        self.decision_client = build_client(profile, profile.decision, key)
        self.client = build_client(profile, profile.response, key)

    async def decide(self, messages: Sequence[BaseMessage]) -> str:
        """调用 JSON 模式；拒绝空值、非正文或截断，错误不泄露供应商原始响应。"""
        try:
            message = await self.decision_client.ainvoke(
                list(messages), response_format={"type": "json_object"}
            )
        except APIError as error:
            raise classify_provider_error(error) from None
        if (
            not isinstance(message.content, str)
            or not message.content.strip()
            or message.response_metadata.get("finish_reason") == "length"
        ):
            raise ModelCallError(
                "MODEL_DECISION_INVALID", "模型未返回完整有效的决策。", True
            )
        return message.content

    async def stream(
        self, messages: Sequence[BaseMessage]
    ) -> AsyncGenerator[str, None]:
        """原生流式读取正文；不转发 additional_kwargs 内的思考内容或响应元数据。"""
        try:
            source = self.client.astream(list(messages))
            try:
                async for chunk in source:
                    if chunk.response_metadata.get("finish_reason") == "length":
                        raise ResearchRuntimeError(
                            "MODEL_OUTPUT_TRUNCATED",
                            "回复超过输出限制，当前内容尚未完整。",
                        )
                    if isinstance(chunk.content, str) and chunk.content:
                        yield chunk.content
            finally:
                close = getattr(source, "aclose", None)
                if close is not None:
                    await close()
        except APIError as error:
            raise classify_provider_error(error) from None
