"""Agent 图运行所需的领域配置。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from internal.entity.app_entity import DEFAULT_APP_CONFIG
from internal.entity.conversation_entity import InvokeFrom

from .subagent_config import SubAgentConfig


class AgentConfig(BaseModel):
    """编译 Deep Agents 图所需的已规范化业务配置。"""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        frozen=True,
    )

    user_id: UUID
    invoke_from: InvokeFrom = InvokeFrom.WEB_APP
    app_name: str = "智能助手"
    app_description: str = "提供准确、可靠的任务协助。"
    preset_prompt: str = ""
    opening_statement: str = ""
    tools: list[BaseTool] = Field(default_factory=list)
    review_config: dict[str, Any] = Field(
        default_factory=lambda: deepcopy(DEFAULT_APP_CONFIG["review_config"])
    )
    max_model_calls: int = Field(default=10, ge=1, strict=True)
    max_tool_calls: int = Field(default=40, ge=1, strict=True)
    memory_enabled: bool = Field(default=False, strict=True)
    selected_skills: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    subagents: list[SubAgentConfig] = Field(default_factory=list)

    @field_validator("review_config", mode="before")
    @classmethod
    def copy_nested_config(cls, value: Any) -> Any:
        """复制嵌套配置，避免运行时修改污染应用配置或其他 Run。"""
        return deepcopy(value)


__all__ = ["AgentConfig"]
