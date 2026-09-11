"""已解析的 Agent 定义，独立于平台应用配置 Schema。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool


@dataclass(frozen=True, slots=True)
class SyncSubAgentDefinition:
    """本轮已授权的同步子 Agent，无需再解析模型来源或工具名称。"""

    name: str
    description: str
    system_prompt: str
    model: BaseChatModel | str
    tools: tuple[BaseTool, ...] = ()
    skills: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AsyncSubAgentDefinition:
    """可直接交给 Agent Protocol 的目标，平台负责解析发布应用和内部认证。"""

    name: str
    description: str
    graph_id: str
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


SubAgentDefinition = SyncSubAgentDefinition | AsyncSubAgentDefinition


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
    subagents: tuple[SubAgentDefinition, ...] = ()
