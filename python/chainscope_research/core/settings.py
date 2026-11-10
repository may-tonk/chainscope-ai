"""显式加载 YAML、环境覆盖及凭证，再交给独立 Pydantic 配置合同校验。"""

import os
from pathlib import Path

import yaml
from dotenv import dotenv_values
from pydantic import SecretStr, ValidationError

from chainscope_research.core.config_schema import (
    ModelConfiguration,
    ResearchSettings,
    RuntimeSettings,
)
from chainscope_research.core.errors import ResearchRuntimeError


class UniqueKeyLoader(yaml.SafeLoader):
    """使用安全 YAML 标签集合，同时禁止重复键被静默覆盖。"""

    def construct_mapping(self, node, deep=False):
        """输入 YAML 映射节点，返回无重复键的字典；错误不打印配置原文。"""
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in mapping:
                raise ValueError("YAML 配置键必须是无重复的字符串")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def load_main_settings(project_root: Path) -> ResearchSettings:
    """读取明确根目录；部署环境覆盖本地 .env，允许的环境覆盖再优先于 YAML。

    不写进程环境，不读取旧 research.toml，不把凭证或完整配置错误返回前端。
    """
    try:
        models = yaml.load(
            (project_root / "config/research_models.yaml").read_text(encoding="utf-8"),
            Loader=UniqueKeyLoader,
        )
        runtime = yaml.load(
            (project_root / "config/research_runtime.yaml").read_text(encoding="utf-8"),
            Loader=UniqueKeyLoader,
        )
        if not isinstance(models, dict) or not isinstance(runtime, dict):
            raise ValueError("配置必须为映射")
        values = {
            **dotenv_values(project_root / ".env", interpolate=False),
            **os.environ,
        }
        # 只覆盖已登记的模型和端点；不把任意环境变量传给 SDK。
        for section, variable in (
            ("primary", "CHAINSCOPE_RESEARCH_MODEL"),
            ("fallback", "CHAINSCOPE_RESEARCH_FALLBACK_MODEL"),
        ):
            override = values.get(variable)
            if section == "primary":
                override = override or values.get("DEEPSEEK_MODEL")
            if override:
                models[section]["model"] = override.strip()
            base_override = values.get("DEEPSEEK_BASE_URL")
            if base_override:
                models[section]["base_url"] = base_override.strip()
        profiles = ModelConfiguration.model_validate(models)
        # 配置校验后才按变量名取密钥，凭证不进入 YAML 或模型尝试记录。
        primary_key = (values.get(profiles.primary.api_key_env) or "").strip()
        fallback_key = (values.get(profiles.fallback.api_key_env) or "").strip()
        if not primary_key or (profiles.fallback_enabled and not fallback_key):
            raise ValueError("缺少模型凭证")
        return ResearchSettings(
            models=profiles,
            runtime=RuntimeSettings.model_validate(runtime),
            primary_key=SecretStr(primary_key),
            fallback_key=SecretStr(fallback_key),
        )
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError, ValidationError):
        raise ResearchRuntimeError(
            "MODEL_CONFIG_INVALID", "请检查研究 YAML 配置、参数兼容性及 DeepSeek 凭证。"
        ) from None
