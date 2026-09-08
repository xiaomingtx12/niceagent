"""已解析的 Agent 定义，独立于平台应用配置 Schema。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from .subagent_config import SubAgentConfig


@dataclass(frozen=True, slots=True)
class SubAgentRuntimeBinding:
    """已解析的子 Agent 模型和工具覆盖，不涉及发布应用查询。"""

    tools: tuple[BaseTool, ...] | None = None
    model: BaseChatModel | str | None = None


@dataclass(frozen=True)
class AgentSkill:
    """Core 可见的只读 Skill 包，不包含平台目录版本和授权记录。"""

    name: str
    skill_md: str
    package_root: Path
    package_files: Mapping[str, str]


@dataclass(frozen=True)
class AgentConfig:
    """模型、工具和 Skill 均由调用方完成授权与解析。"""

    name: str
    description: str
    model: BaseChatModel
    preset_prompt: str
    review_config: Mapping[str, Any] = field(default_factory=dict)
    tools: tuple[BaseTool, ...] = ()
    skills: tuple[AgentSkill, ...] = ()
    selected_skills: tuple[str, ...] = ()
    subagents: tuple[SubAgentConfig, ...] = ()
    subagent_bindings: Mapping[str, SubAgentRuntimeBinding] = field(default_factory=dict)
