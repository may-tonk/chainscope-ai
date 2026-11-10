"""按 YAML 名称加载 TXT 并执行受限占位符替换；不执行模板代码或持久保存会话。

加载时冻结正文与元信息；每轮只传变量值，不缓存已填入用户数据的结果。
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

import yaml
from pydantic import ValidationError

from chainscope_research.contracts.main_agent import PromptReference
from chainscope_research.core.config_schema import PromptConfiguration
from chainscope_research.core.errors import ResearchRuntimeError
from chainscope_research.core.settings import UniqueKeyLoader

# 只识别约定的 {{snake_case}}。普通 JSON 的单层花括号无需转义。
VARIABLE = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")


@dataclass(frozen=True)
class LoadedPrompt:
    """不可变的已加载模板，正文不进入 repr，避免错误输出时展开系统提示。"""

    reference: PromptReference
    required_variables: tuple[str, ...]
    content: str = field(repr=False)

    def require_variables(self, expected: tuple[str, ...]) -> None:
        """由调用方在装配时声明其参数合同，及时发现 YAML 与代码不匹配。"""
        if set(expected) != set(self.required_variables):
            raise ResearchRuntimeError(
                "PROMPT_VARIABLES_INVALID",
                f"提示词 {self.reference.name} 的变量与调用合同不一致。",
            )

    def render(self, **variables: str) -> str:
        """输入已按业务合同准备的字符串参数，返回单次渲染结果，不修改原模板。

        仅做一遍替换：变量值中的 {{其他变量}} 不再解释，避免注入内容递归展开。
        缺参、多参、非字符串值都会报错；不把传入值写进错误或 YAML。
        """
        if set(variables) != set(self.required_variables) or any(
            not isinstance(value, str) or not value.strip()
            for value in variables.values()
        ):
            raise ResearchRuntimeError(
                "PROMPT_VARIABLES_INVALID",
                f"提示词 {self.reference.name} 的传参缺失、多余或格式错误。",
            )
        return VARIABLE.sub(lambda match: variables[match.group(1)], self.content)


@dataclass(frozen=True)
class PromptRegistry:
    """只读模板目录；不在 import 时加载，不创建全局可变缓存。"""

    entries: Mapping[str, LoadedPrompt]

    def get(self, name: str) -> LoadedPrompt:
        """输入程序登记名称，返回模板；未知名称不回退到任意默认提示词。"""
        if name not in self.entries:
            raise ResearchRuntimeError(
                "PROMPT_NOT_REGISTERED", "所需提示词未在登记表中配置。"
            )
        return self.entries[name]


def load_prompt_registry(project_root: Path) -> PromptRegistry:
    """输入项目根目录，加载并校验 YAML 与所有登记 TXT，返回不可变目录。

    路径在解析符号链接后仍须位于本项目 prompts/；不能读取 .env 或其他文件。
    用户与模型不能传路径，只能由开发者修改本地登记表后重新装配资源。
    """
    try:
        root = project_root.resolve(strict=True)
        prompt_root = (root / "prompts").resolve(strict=True)
        if not prompt_root.is_relative_to(root):
            raise ValueError("提示词根目录越界")
        raw = yaml.load(
            (root / "config/research_prompts.yaml").read_text(encoding="utf-8"),
            Loader=UniqueKeyLoader,
        )
        config = PromptConfiguration.model_validate(raw)
        templates = {}
        for name, specification in config.prompts.items():
            declared = Path(specification.file)
            if declared.is_absolute():
                raise ValueError("需要相对路径")
            path = (root / declared).resolve(strict=True)
            if not path.is_relative_to(prompt_root) or path.suffix != ".txt":
                raise ValueError("提示词路径越界或类型错误")
            content = path.read_text(encoding="utf-8")
            if not content.strip():
                raise ValueError("提示词正文为空")
            actual_variables = set(VARIABLE.findall(content))
            if "{{" in VARIABLE.sub("", content) or actual_variables != set(
                specification.required_variables
            ):
                raise ValueError("提示词占位符与声明不一致")
            templates[name] = LoadedPrompt(
                reference=PromptReference(
                    name=name,
                    version=specification.version,
                    content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                ),
                required_variables=tuple(specification.required_variables),
                content=content,
            )
        return PromptRegistry(MappingProxyType(templates))
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        yaml.YAMLError,
        ValidationError,
    ):
        # 不输出原始 YAML、TXT 或参数，避免把系统提示、用户数据带到前端。
        raise ResearchRuntimeError(
            "PROMPT_CONFIG_INVALID", "请检查提示词登记表、TXT 路径及占位符声明。"
        ) from None
