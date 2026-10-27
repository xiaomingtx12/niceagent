"""内置 Skill 领域对象。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BuiltinSkill:
    """从内置目录加载并经过摘要校验的 Skill 元数据。"""

    name: str
    description: str
    version: str
    digest: str
    skill_md: str
    package_root: Path
    package_files: Mapping[str, str]
    category: str = "agent"
    execution_mode: str = "inline"
    requires: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    enabled: bool = True


__all__ = ["BuiltinSkill"]
