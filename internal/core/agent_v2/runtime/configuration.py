"""平台 AppConfig 到 AgentConfig 的统一转换边界。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from langchain_core.tools import BaseTool

from internal.core.agent_v2.agents.models import AgentConfig
from internal.entity.app_entity import DEFAULT_APP_CONFIG
from internal.entity.conversation_entity import InvokeFrom


def build_agent_config_from_app_config(
    *,
    user_id: UUID,
    invoke_from: InvokeFrom,
    app_name: str,
    app_description: str,
    app_config: Mapping[str, Any],
    tools: Sequence[BaseTool],
    opening_statement: str = "",
) -> AgentConfig:
    """把前端或已发布 AppConfig 转为 Agent 配置和展示数据。"""
    # 1.读取应用级提示词、工具和运行限制。
    # 2.补齐 SubAgent、审查配置和默认值。
    # 3.统一封装为 BaseAgent 使用的 AgentConfig。
    return AgentConfig(
        user_id=user_id,
        invoke_from=invoke_from,
        app_name=app_name,
        app_description=app_description,
        preset_prompt=str(app_config.get("preset_prompt", "") or ""),
        opening_statement=opening_statement,
        subagents=app_config.get("subagents") or [],
        tools=list(tools),
        review_config=(
            app_config.get("review_config")
            or DEFAULT_APP_CONFIG["review_config"]
        ),
        max_model_calls=int(app_config.get("max_model_calls", 10)),
        max_tool_calls=int(app_config.get("max_tool_calls", 40)),
    )


__all__ = ["build_agent_config_from_app_config"]
