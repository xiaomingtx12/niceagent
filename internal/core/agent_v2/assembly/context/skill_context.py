"""Skill 运行时资源装配。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import BackendProtocol

from internal.core.agent_v2.entity.agent_config import AgentSkill, SubAgentDefinition, SyncSubAgentDefinition
from internal.extension.logging_extension import logger


@dataclass
class SkillProjection:
    """持有本轮已授权 Skill 的临时文件；读取交给官方 Backend，访问策略交给权限。"""

    routes: dict[str, BackendProtocol] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    subagent_sources: dict[str, list[str]] = field(default_factory=dict)
    files: list[tuple[str, bytes]] = field(default_factory=list)
    _directory: TemporaryDirectory | None = field(default=None, init=False, repr=False)

    def prepare(self, packages: Mapping[str, list[tuple[str, bytes]]]) -> None:
        """为每个授权范围提供独立目录根，运行资源统一持有整份投影。"""
        if not packages:
            return
        # 1.一份投影只创建一个临时目录，每个路由使用独立子目录防止越界读取。
        self._directory = TemporaryDirectory(prefix="nice-agent-skills-")
        try:
            for index, (prefix, files) in enumerate(packages.items()):
                root = (Path(self._directory.name) / str(index)).resolve()
                root.mkdir()
                for path, content in files:
                    target = (root / path.lstrip("/")).resolve()
                    if not target.is_relative_to(root) or target == root:
                        raise ValueError(f"invalid Skill projection path: {path}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                # 2.Backend 仅提供文件能力，模型写入由 FilesystemPermission 拒绝。
                self.routes[prefix] = FilesystemBackend(root_dir=root, virtual_mode=True)
                self.files.extend((f"{prefix.rstrip('/')}{path}", content) for path, content in files)
        except BaseException:
            try:
                self.close()
            except BaseException:
                logger.exception("Skill 投影创建失败后的临时目录清理失败")
            raise

    def close(self) -> None:
        """统一释放临时文件，允许空投影和重复清理；Backend 不承担目录生命周期。"""
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None


def build_skill_context(
    *,
    skills: Sequence[AgentSkill],
    subagents: Sequence[SubAgentDefinition],
    selected_skill_names: Sequence[str] = (),
) -> SkillProjection:
    """装配已授权 Skill 路由，文件工具的只读约束由框架权限统一负责。"""
    _validate_skills(skills)
    skill_map = {skill.name: skill for skill in skills}
    selected_names = set(selected_skill_names)
    unknown_selected = sorted(selected_names - set(skill_map))
    if unknown_selected:
        raise ValueError(
            "选择了未启用的 Skills: " + ", ".join(unknown_selected)
        )

    projection = SkillProjection()
    packages: dict[str, list[tuple[str, bytes]]] = {}
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

    for subagent_config in subagents:
        if not isinstance(subagent_config, SyncSubAgentDefinition):
            continue
        requested = subagent_config.skills
        if len(requested) != len(set(requested)):
            raise ValueError(f"subagent {subagent_config.name!r} 的 Skills 重复")
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
    projection.prepare(packages)
    return projection



def _package_files(*, root: str, skill: AgentSkill) -> list[tuple[str, bytes]]:
    """把可信 Skill 包转换为 Sandbox 上传文件。"""
    package_files = skill.package_files or {
        "SKILL.md": skill.skill_md,
    }
    files: list[tuple[str, bytes]] = []
    for relative, content in package_files.items():
        # 1.规范化分隔符并拒绝绝对路径或目录回退。
        normalized = str(relative).replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            not normalized.strip("/")
            or path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError(f"invalid Skill package path: {relative}")
        # 2.生成固定根目录下的虚拟路径，并统一编码为字节内容。
        files.append((f"{root.rstrip('/')}/{'/'.join(path.parts)}", str(content).encode("utf-8")))
    return files


def _validate_skills(skills: Sequence[AgentSkill]) -> None:
    """在构造路由前拒绝重名或非法名称，避免路由覆盖和路径逃逸。"""
    names: set[str] = set()
    duplicates: set[str] = set()
    for skill in skills:
        # 1.名称同时作为路由目录，必须保持单段且无空白变体。
        name = str(skill.name).strip()
        if (
            not name
            or name != skill.name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
        ):
            raise ValueError(f"invalid Skill name: {skill.name!r}")
        if name in names:
            duplicates.add(name)
        names.add(name)
    # 2.重复名称会导致路由与授权对象不确定，统一拒绝启动。
    if duplicates:
        raise ValueError("Skill 名称重复: " + ", ".join(sorted(duplicates)))


__all__ = ["build_skill_context", "SkillProjection"]
