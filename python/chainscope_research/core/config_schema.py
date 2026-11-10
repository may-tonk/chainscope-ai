"""YAML 配置的 Pydantic 合同；与 contracts/ 中业务传输合同分开维护。

只开放已接入的参数，拒绝未知字段和不会生效的参数组合，密钥不写入 YAML。
"""

from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, StringConstraints, model_validator

from chainscope_research.contracts.common import Contract, NonEmptyText

PromptName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]


class PromptSpecification(Contract):
    """一份 TXT 的登记信息；只声明变量名，运行时值由对应调用代码提供。"""

    file: NonEmptyText = Field(description="相对项目根目录的 prompts/ 内 TXT 路径")
    version: NonEmptyText = Field(
        description="人工维护的提示词版本，不是业务 Schema 版本"
    )
    required_variables: list[PromptName] = Field(
        description="正文使用的全部占位符名称，不包含参数值"
    )

    @model_validator(mode="after")
    def check_unique_variables(self) -> Self:
        """输入变量名列表，拒绝重复声明；允许无变量的静态模板。"""
        if len(set(self.required_variables)) != len(self.required_variables):
            raise ValueError("提示词变量不能重复声明")
        return self


class PromptConfiguration(Contract):
    """YAML 提示词登记合同；允许后续添加条目，不隐式启用任何 Agent。"""

    prompts: dict[PromptName, PromptSpecification] = Field(min_length=1)


class GenerationSettings(Contract):
    """单个调用阶段的生成设置；流式正文和 JSON 决策的协议仍由代码强制。"""

    temperature: float | None = Field(
        default=0.0,
        ge=0,
        le=2,
        description="采样温度；thinking 开启或改用 top_p 时填 null",
    )
    top_p: float | None = Field(
        default=None, gt=0, le=1, description="可选核采样概率，须通过模型模式兼容性检查"
    )
    max_tokens: int = Field(
        ge=128, le=8192, description="当前阶段最大生成 token，不是字符数"
    )
    thinking: Literal["enabled", "disabled"] = Field(
        default="disabled", description="供应商思考模式；思考内容不作为正文输出"
    )

    @model_validator(mode="after")
    def check_sampling(self) -> Self:
        """校验采样组合，避免声明了配置但供应商忽略其效果。"""
        if self.thinking == "enabled" and self.temperature is not None:
            raise ValueError("thinking 开启时 temperature 必须为 null")
        if self.temperature is not None and self.top_p is not None:
            raise ValueError("temperature 与 top_p 仅选择一项配置")
        return self


class ModelProfile(Contract):
    """主备模型分别配置阶段参数，供应商适配范围明确限定为 DeepSeek 官方。"""

    # 模型枚举就是本次验收范围；新增模型需先增加能力校验和兼容验证。
    provider: Literal["deepseek"]
    model: Literal["deepseek-flash", "deepseek-v4-pro"]
    base_url: NonEmptyText
    api_key_env: NonEmptyText
    request_timeout_seconds: int = Field(gt=0, le=120)
    # 当前统一尝试预算管理所有重试，禁止 SDK 自己叠加请求。
    sdk_max_retries: int = Field(default=0, ge=0, le=0)
    decision: GenerationSettings
    response: GenerationSettings

    @model_validator(mode="after")
    def check_provider(self) -> Self:
        """检查接口和模型能力，不发送凭证到非官方地址，不接受无效采样配置。"""
        address = urlsplit(self.base_url)
        if (
            address.scheme != "https"
            or address.hostname != "api.deepseek.com"
            or address.port not in (None, 443)
            or address.username
            or address.password
            or address.query
            or address.fragment
            or address.path not in ("", "/", "/v1", "/v1/")
        ):
            raise ValueError("仅支持 DeepSeek 官方 HTTPS 接口")
        for generation in (self.decision, self.response):
            if (
                self.model == "deepseek-v4-pro"
                and generation.thinking == "enabled"
                and generation.top_p is not None
            ):
                raise ValueError("pro thinking 模式的 top_p 固定，由供应商决定")
            if self.model == "deepseek-flash" and generation.top_p is not None:
                if generation.thinking == "disabled" or generation.top_p < 0.95:
                    raise ValueError("flash 当前模式不支持所配置的 top_p")
        return self


class ModelConfiguration(Contract):
    """两个预先登记的配置，备用不产生新 Agent 或新权限。"""

    fallback_enabled: bool
    primary: ModelProfile
    fallback: ModelProfile

    @model_validator(mode="after")
    def check_distinct(self) -> Self:
        """启用备用时必须是不同模型，避免换名称后实际重复请求同一模型。"""
        if self.fallback_enabled and self.primary.model == self.fallback.model:
            raise ValueError("主备模型不能相同")
        return self


class RuntimeSettings(Contract):
    """单轮统一预算；决策和正文共同计数，正文发出后不再透明切换。"""

    turn_timeout_seconds: int = Field(
        gt=0, le=1000, description="决策、正文和切换共享的整轮秒数，上限 1000"
    )
    max_input_characters: int = Field(
        ge=1000, le=100000, description="本轮所有消息正文的总字符数"
    )
    max_output_characters: int = Field(
        ge=1000, le=100000, description="本轮正文累计字符上限"
    )
    max_attempts_per_stage: int = Field(
        ge=1, le=2, description="每阶段尝试总次数，含首次调用"
    )
    max_attempts_per_turn: int = Field(
        ge=2, le=4, description="两阶段合计尝试总次数，失败也计入"
    )


class ResearchSettings(Contract):
    """装配后的不可变设置；凭证仅保存在排除序列化的字段。"""

    models: ModelConfiguration
    runtime: RuntimeSettings
    primary_key: SecretStr = Field(exclude=True, repr=False)
    fallback_key: SecretStr = Field(exclude=True, repr=False)
