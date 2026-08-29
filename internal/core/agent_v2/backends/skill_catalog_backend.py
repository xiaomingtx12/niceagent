"""已授权 Skill 包的 DeepAgents 只读文件投影。"""

from __future__ import annotations

from typing import Any

from internal.core.agent_v2.backends.read_only_catalog_backend import (
    ReadOnlyCatalogBackend,
)


class SkillCatalogBackend(ReadOnlyCatalogBackend):
    """向原生 SkillsMiddleware 暴露已启用 Skill 包。"""

    def __init__(self, skills: list[Any]) -> None:
        """从已启用记录构造只读目录。"""
        files: dict[str, str] = {}
        for skill in skills:
            if not skill.enabled:
                continue
            package_files = getattr(skill, "package_files", None)
            if package_files is None:
                package_files = {"SKILL.md": skill.skill_md}
            files.update(
                {
                    f"/{skill.name}/{relative}": content
                    for relative, content in package_files.items()
                }
            )
        super().__init__(
            files,
            missing_label="skill file",
            read_only_label="Skill catalog",
        )


__all__ = ["SkillCatalogBackend"]
