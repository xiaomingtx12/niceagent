"""Skill 运行时资源装配。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import BackendProtocol

from internal.exception import SubAgentConfigError


@dataclass
class SkillProjection:
    """主 Agent 和 SubAgent 可见的 Skill 投影。"""

    routes: dict[str, BackendProtocol] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    subagent_sources: dict[str, list[str]] = field(default_factory=dict)
    files: list[tuple[str, bytes]] = field(default_factory=list)


class SkillContextAssembler:
    """把已授权 Skill 投影成 DeepAgents 原生路径。"""

    def build(
        self,
        *,
        skills: list[Any],
        subagents: list[dict[str, Any]] | None,
        selected_skill_names: Sequence[str] = (),
    ) -> SkillProjection:
        """装配主 Agent 和 SubAgent 的只读 Skill 路由。"""
        projection = SkillProjection()
        enabled_skills = [skill for skill in skills if getattr(skill, "enabled", True)]
        selected_names = set(selected_skill_names)
        main_skills = (
            [skill for skill in enabled_skills if skill.name in selected_names]
            if selected_names
            else enabled_skills
        )
        if main_skills:
            projection.routes["/skills/"] = _filesystem_backend(main_skills)
            projection.sources = ["/skills/"]
            for skill in main_skills:
                projection.files.extend(
                    _package_files(root=f"/skills/{skill.name}", skill=skill)
                )

        skill_map = {skill.name: skill for skill in enabled_skills}
        for subagent_config in subagents or []:
            requested = subagent_config["skills"]
            missing = sorted(set(requested) - set(skill_map))
            if missing:
                raise SubAgentConfigError(
                    f"subagent {subagent_config['name']!r} 引用了未启用的 Skills: "
                    f"{', '.join(missing)}"
                )
            if not requested:
                continue
            name = subagent_config["name"]
            prefix = f"/subagent_skills/{name}/"
            selected = [skill_map[item] for item in requested]
            projection.routes[prefix] = _filesystem_backend(selected)
            projection.subagent_sources[name] = [prefix]
            for skill in selected:
                projection.files.extend(
                    _package_files(root=f"{prefix}{skill.name}", skill=skill)
                )
        return projection


def _package_files(*, root: str, skill: Any) -> list[tuple[str, bytes]]:
    """把可信 Skill 包转换为 Sandbox 上传文件。"""
    package_files = getattr(skill, "package_files", None) or {
        "SKILL.md": skill.skill_md,
    }
    files: list[tuple[str, bytes]] = []
    for relative, content in package_files.items():
        normalized = str(relative).replace("\\", "/").strip("/")
        if (
            not normalized
            or normalized.startswith("../")
            or "/../" in normalized
            or normalized in {".", ".."}
        ):
            raise ValueError(f"invalid Skill package path: {relative}")
        files.append((f"{root.rstrip('/')}/{normalized}", str(content).encode("utf-8")))
    return files


def _filesystem_backend(skills: Sequence[Any]) -> FilesystemBackend:
    """把内置 Skill 根目录接入 DeepAgents 文件 Backend。"""
    roots = {
        Path(skill.package_root).resolve().parent
        for skill in skills
        if getattr(skill, "package_root", None) is not None
    }
    if len(roots) != 1:
        raise ValueError("enabled Skills must share one package root")
    return FilesystemBackend(root_dir=next(iter(roots)), virtual_mode=True)


__all__ = ["SkillContextAssembler", "SkillProjection"]
