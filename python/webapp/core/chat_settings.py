"""网页聊天的类型化运行设置；复用研究 YAML 加载器，不维护重复模型参数。"""

import yaml
from pydantic import Field

from chainscope_research.contracts.common import Contract
from chainscope_research.core.settings import UniqueKeyLoader

from .errors import ConfigError
from .settings import ROOT


class ChatSettings(Contract):
    """API 实例的资源和传输限制；实际值统一从 web_chat.yaml 装配。"""

    max_parallel_turns: int = Field(ge=1)
    max_turns_per_user: int = Field(ge=1)
    max_messages: int = Field(ge=1)
    heartbeat_seconds: int = Field(ge=1)
    send_timeout_seconds: int = Field(ge=1)
    reauth_seconds: int = Field(ge=1)


def load_chat_settings() -> ChatSettings:
    """显式读取本项目 YAML，错误不回显配置正文或秘密。"""
    try:
        raw = yaml.load(
            (ROOT / "config/web_chat.yaml").read_text(encoding="utf-8"),
            Loader=UniqueKeyLoader,
        )
        return ChatSettings.model_validate(raw)
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        raise ConfigError("请检查网页聊天的 YAML 设置") from None
