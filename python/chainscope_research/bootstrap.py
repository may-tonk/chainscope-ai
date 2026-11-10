"""研究包资源装配：显式读取配置和提示词，不在导入或服务启动时建表。"""

from pathlib import Path

from chainscope_research.application import MainAgentApplication
from chainscope_research.core.prompt_loader import load_prompt_registry
from chainscope_research.core.settings import load_main_settings
from chainscope_research.model.factory import DeepSeekMainModel
from chainscope_research.model.fallback import ResilientModels


def build_analysis_application(
    project_root: Path, authorizer, cursor_secret, *, engine=None
):
    """装配四个真实工具；要求显式服务端授权与稳定分页密钥，不在启动时建表。

    不复用采集写库入口。测试可注入隔离 Engine；调用方最后 await aclose()。
    数据库账号本身应在部署时限制 SELECT，本实现额外使用只读事务防护。
    """
    from chainscope_research.core.tool_settings import load_analysis_settings
    from chainscope_research.services.data_tools import AnalysisToolService
    from chainscope_research.services.event_cursor import EventCursor
    from chainscope_research.storage.repositories.chain_facts import (
        ReadonlyFacts,
        create_read_engine,
    )

    settings = load_analysis_settings(project_root)
    cursor = EventCursor(cursor_secret)
    owned = engine is None
    actual_engine = (
        engine if engine is not None else create_read_engine(project_root, settings)
    )
    return AnalysisToolService(
        ReadonlyFacts(actual_engine), authorizer, settings, cursor, owns_engine=owned
    )


def build_main_application(project_root: Path) -> MainAgentApplication:
    """输入明确项目根目录，返回真实模型应用；缺配置或提示词时报安全错误。"""
    settings = load_main_settings(project_root)
    registry = load_prompt_registry(project_root)
    decision = registry.get("main_decision")
    response = registry.get("main_response")
    models = [
        (
            settings.models.primary.model,
            DeepSeekMainModel(settings.models.primary, settings.primary_key),
        )
    ]
    if settings.models.fallback_enabled:
        models.append(
            (
                settings.models.fallback.model,
                DeepSeekMainModel(settings.models.fallback, settings.fallback_key),
            )
        )
    return MainAgentApplication(
        ResilientModels(tuple(models), settings.runtime),
        decision,
        response,
        turn_timeout_seconds=settings.runtime.turn_timeout_seconds,
        max_input_characters=settings.runtime.max_input_characters,
        max_output_characters=settings.runtime.max_output_characters,
    )
