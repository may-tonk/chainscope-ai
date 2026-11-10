"""四类分析工具的 YAML 合同与显式加载；不在导入时连接数据库。"""

from pathlib import Path
from typing import Self

import yaml
from pydantic import Field, model_validator

from chainscope_research.contracts.common import Contract, PositiveInt
from chainscope_research.contracts.tools import ToolId
from chainscope_research.core.errors import ResearchRuntimeError
from chainscope_research.core.settings import UniqueKeyLoader

TOOL_IDS = frozenset(
    (
        "lookup_transaction_events",
        "lookup_block_events",
        "analyze_address_activity",
        "analyze_token_activity",
    )
)


class ToolPolicy(Contract):
    """单工具启用与整次调用时间；启用不替代服务端授权。"""

    enabled: bool
    timeout_seconds: PositiveInt


class AnalysisSettings(Contract):
    """查询和计算资源上限的唯一配置入口，各数值单位见 YAML 中文注释。"""

    max_parallel_calls: PositiveInt
    max_analysis_rows: PositiveInt
    max_periods: PositiveInt
    evidence_limit: int = Field(ge=0)
    top_addresses: PositiveInt
    connect_timeout_seconds: PositiveInt
    read_timeout_seconds: PositiveInt
    cursor_ttl_seconds: PositiveInt
    tools: dict[ToolId, ToolPolicy]

    @model_validator(mode="after")
    def check_tools(self) -> Self:
        """要求四个能力均有明确策略，不缺项时偷偷使用默认限制。"""
        if set(self.tools) != TOOL_IDS:
            raise ValueError("四个工具必须全部声明运行策略")
        return self


def load_analysis_settings(root: Path) -> AnalysisSettings:
    """从项目根目录加载安全 YAML 并验证；不打印原始配置或凭证。"""
    try:
        raw = yaml.load(
            (root / "config/research_tools.yaml").read_text(encoding="utf-8"),
            Loader=UniqueKeyLoader,
        )
        return AnalysisSettings.model_validate(raw)
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        raise ResearchRuntimeError(
            "TOOL_CONFIG_INVALID", "请检查研究工具 YAML 设置。"
        ) from None
