"""内置 Skill 文件系统适配器。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
from types import MappingProxyType

from injector import inject
from pydantic import ValidationError
import yaml

from config import Config
from internal.entity import BuiltinSkill
from internal.schema.skill.skill_schema import (
    BuiltinSkillManifestReq,
    normalize_manifest_string_list,
)


_SKILL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_MAX_PACKAGE_FILES = 256
_MAX_PACKAGE_BYTES = 20 * 1024 * 1024
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class BuiltinSkillRepositoryError(RuntimeError):
    """内置 Skill 目录配置或包内容不合法。"""


@inject
@dataclass
class BuiltinSkillRepository:
    """加载并校验随应用发布的内置 Skill Catalog。"""

    config: Config
    _catalog: Mapping[str, BuiltinSkill] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def load_catalog(self) -> Mapping[str, BuiltinSkill]:
        """首次访问时完整加载 Catalog，后续复用不可变快照。"""
        if self._catalog is None:
            try:
                self._catalog = MappingProxyType(self._scan_catalog())
            except BuiltinSkillRepositoryError:
                raise
            except OSError as exc:
                raise BuiltinSkillRepositoryError("内置 Skill 目录读取失败") from exc
        return self._catalog

    def _scan_catalog(self) -> dict[str, BuiltinSkill]:
        """扫描根目录，并将每个 Skill 包一次性读取为领域对象。"""
        root = Path(self.config.BUILTIN_SKILLS_ROOT)
        if not root.is_absolute():
            root = _PROJECT_ROOT / root
        if not root.exists():
            raise BuiltinSkillRepositoryError(
                "BUILTIN_SKILLS_ROOT 配置的目录不存在"
            )
        if not root.is_dir():
            raise BuiltinSkillRepositoryError("BUILTIN_SKILLS_ROOT 必须是目录")

        catalog: dict[str, BuiltinSkill] = {}
        for package_root in sorted(root.iterdir()):
            if not package_root.is_dir() or package_root.is_symlink():
                continue
            skill = self._load_skill(package_root)
            catalog[skill.name] = skill
        return catalog

    def _load_skill(self, package_root: Path) -> BuiltinSkill:
        """在一次文件遍历中完成安全校验、文本加载和摘要计算。"""
        if not _SKILL_NAME.fullmatch(package_root.name):
            raise BuiltinSkillRepositoryError(
                f"内置 Skill 目录名非法: {package_root.name}"
            )

        package_files, digest = _load_text_package(package_root)
        skill_md = package_files.get("SKILL.md")
        if skill_md is None:
            raise BuiltinSkillRepositoryError(
                f"内置 Skill 缺少 SKILL.md: {package_root.name}"
            )

        manifest = _parse_frontmatter(skill_md)
        if manifest.name != package_root.name:
            raise BuiltinSkillRepositoryError(
                f"Skill name 必须与目录名一致: {package_root.name}"
            )
        metadata = manifest.metadata.my_dify
        return BuiltinSkill(
            name=manifest.name,
            description=manifest.description,
            version=metadata.version,
            digest=digest,
            skill_md=skill_md,
            package_root=package_root.resolve(),
            package_files=MappingProxyType(package_files),
            category=metadata.category.strip().lower(),
            execution_mode=metadata.execution_mode,
            requires=normalize_manifest_string_list(metadata.requires),
            allowed_tools=normalize_manifest_string_list(manifest.allowed_tools),
        )


def _load_text_package(root: Path) -> tuple[dict[str, str], str]:
    """单次遍历并读取 UTF-8 文件，同时计算稳定摘要和包大小。"""
    files: dict[str, str] = {}
    hasher = hashlib.sha256()
    total_bytes = 0

    for entry in sorted(root.rglob("*")):
        if entry.is_symlink():
            raise BuiltinSkillRepositoryError(
                f"内置 Skill 不允许符号链接: {entry}"
            )
        if _is_runtime_cache(entry, root) or entry.is_dir():
            continue
        if not entry.is_file():
            raise BuiltinSkillRepositoryError(
                f"内置 Skill 只允许普通文件: {entry}"
            )

        file_count = len(files) + 1
        total_bytes += entry.stat().st_size
        if file_count > _MAX_PACKAGE_FILES or total_bytes > _MAX_PACKAGE_BYTES:
            raise BuiltinSkillRepositoryError(
                f"内置 Skill 包超过文件或字节上限: {root.name}"
            )

        content = entry.read_bytes()
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BuiltinSkillRepositoryError(
                f"内置 Skill 文件必须是 UTF-8 文本: {entry}"
            ) from exc

        relative = entry.relative_to(root).as_posix()
        files[relative] = text
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(content)
        hasher.update(b"\0")

    return files, f"sha256:{hasher.hexdigest()}"


def _parse_frontmatter(skill_md: str) -> BuiltinSkillManifestReq:
    """解析并校验 SKILL.md 顶部的 YAML frontmatter。"""
    lines = skill_md.splitlines()
    if not lines or lines[0].strip() != "---":
        raise BuiltinSkillRepositoryError("SKILL.md 必须包含 YAML frontmatter")
    try:
        end = lines[1:].index("---") + 1
    except ValueError as exc:
        raise BuiltinSkillRepositoryError("SKILL.md frontmatter 未闭合") from exc
    try:
        data = yaml.safe_load("\n".join(lines[1:end])) or {}
        return BuiltinSkillManifestReq.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise BuiltinSkillRepositoryError(
            f"SKILL.md frontmatter 格式错误: {exc}"
        ) from exc


def _is_runtime_cache(path: Path, root: Path) -> bool:
    """识别不属于发布内容的 Python 运行时缓存。"""
    relative = path.relative_to(root)
    return "__pycache__" in relative.parts or path.suffix == ".pyc"


__all__ = ["BuiltinSkillRepository", "BuiltinSkillRepositoryError"]
