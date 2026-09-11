"""Skill 运行时资源装配。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from deepagents.backends.protocol import BackendProtocol

from internal.core.agent_v2.backends.skill_backend import SkillBackend
from internal.core.agent_v2.entity.agent_config import AgentSkill, SubAgentDefinition, SyncSubAgentDefinition


@dataclass
class SkillProjection:
    """主 Agent 和 SubAgent 可见的 Skill 投影。"""

    routes: dict[str, BackendProtocol] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    subagent_sources: dict[str, list[str]] = field(default_factory=dict)
    files: list[tuple[str, bytes]] = field(default_factory=list)


def build_skill_context(
    *,
    skills: Sequence[AgentSkill],
    subagents: Sequence[SubAgentDefinition],
    selected_skill_names: Sequence[str] = (),
) -> SkillProjection:
    """装配主 Agent 和 SubAgent 的只读 Skill 路由。"""
    projection = SkillProjection()
    packages: dict[str, list[tuple[str, bytes]]] = {}
    selected_names = set(selected_skill_names)
    main_skills = (
        [skill for skill in skills if skill.name in selected_names]
        if selected_names
        else skills
    )
    if main_skills:
        packages["/skills/"] = [
            file for skill in main_skills
            for file in _package_files(root=f"/{skill.name}", skill=skill)
        ]
        projection.sources = ["/skills/"]

    skill_map = {skill.name: skill for skill in skills}
    for subagent_config in subagents:
        if not isinstance(subagent_config, SyncSubAgentDefinition):
            continue
        requested = subagent_config.skills
        missing = sorted(set(requested) - set(skill_map))
        if missing:
            raise ValueError(
                f"subagent {subagent_config.name!r} 引用了未启用的 Skills: "
                f"{', '.join(missing)}"
            )
        if not requested:
            continue
        name = subagent_config.name
        prefix = f"/subagent_skills/{name}/"
        selected = [skill_map[item] for item in requested]
        packages[prefix] = [
            file for skill in selected
            for file in _package_files(root=f"/{skill.name}", skill=skill)
        ]
        projection.subagent_sources[name] = [prefix]
    try:
        for prefix, files in packages.items():
            projection.routes[prefix] = SkillBackend(files)
            projection.files.extend((f"{prefix.rstrip('/')}{path}", content) for path, content in files)
    except Exception:
        for backend in projection.routes.values():
            backend.close()
        raise
    return projection



def _package_files(*, root: str, skill: AgentSkill) -> list[tuple[str, bytes]]:
    """把可信 Skill 包转换为 Sandbox 上传文件。"""
    package_files = skill.package_files or {
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


__all__ = ["build_skill_context", "SkillProjection"]
