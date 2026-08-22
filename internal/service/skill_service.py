"""Builtin Skill catalog and AppConfig binding validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from config.config import Config
from internal.exception import FailException, NotFoundException
from .base_service import BaseService


_SKILL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_MAX_PACKAGE_FILES = 256
_MAX_PACKAGE_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class BuiltinSkill:
    name: str
    description: str
    version: str
    digest: str
    skill_md: str
    package_root: Path
    package_files: Mapping[str, str]
    category: str = "general"
    execution_mode: str = "inline"
    requires: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    enabled: bool = True

    def to_dict(self, *, enabled: bool | None = None) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "digest": self.digest,
            "category": self.category,
            "execution_mode": self.execution_mode,
            "requires": list(self.requires),
            "allowed_tools": list(self.allowed_tools),
            "enabled": self.enabled if enabled is None else enabled,
        }


class SkillService(BaseService):
    """Read-only platform Skill catalog.

    Skill content is loaded from the versioned builtin directory. AppConfig and
    AppConfigVersion store binding references only.
    """

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs
        settings = Config()
        self._root = Path(settings.BUILTIN_SKILLS_ROOT)

    def list_catalog(self) -> list[BuiltinSkill]:
        return sorted(self._scan().values(), key=lambda item: item.name)

    def get_catalog_skill(self, name: str) -> BuiltinSkill:
        """按合法名称读取单个内置 Skill。"""
        if not _SKILL_NAME.fullmatch(name):
            raise NotFoundException("Skill 模板不存在")
        skill = self._scan().get(name)
        if skill is None:
            raise NotFoundException("Skill 模板不存在")
        return skill

    def list_skills(
        self,
        *,
        account_id,
        app_id,
        bindings: list[dict[str, Any]] | None = None,
        include_versions: bool = False,
    ) -> list[dict[str, Any]]:
        del account_id, app_id, include_versions
        normalized = self.normalize_bindings(bindings or [])
        enabled_names = {item["name"] for item in normalized if item["enabled"]}
        return [
            skill.to_dict(enabled=skill.name in enabled_names)
            for skill in self.list_catalog()
        ]

    def list_enabled_latest(
        self,
        *,
        account_id,
        app_id,
        bindings: list[dict[str, Any]] | None = None,
    ) -> list[BuiltinSkill]:
        del account_id, app_id
        catalog = self._scan()
        selected: list[BuiltinSkill] = []
        for binding in self.normalize_bindings(bindings or []):
            if not binding["enabled"]:
                continue
            selected.append(catalog[binding["name"]])
        return selected

    def normalize_bindings(
        self,
        bindings: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        if not bindings:
            return []
        if not isinstance(bindings, list):
            raise FailException("Skill 绑定必须是列表")
        catalog = self._scan()
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in bindings:
            if not isinstance(raw, dict):
                raise FailException("Skill 绑定项必须是对象")
            name = str(raw.get("name", "")).strip()
            if name in seen:
                raise FailException("Skill 绑定不能重复")
            if name not in catalog:
                raise FailException(f"Skill 不存在或未内置: {name}")
            skill = catalog[name]
            version = str(raw.get("version") or skill.version)
            digest = str(raw.get("digest") or skill.digest)
            if version != skill.version or digest != skill.digest:
                raise FailException(f"Skill 版本或摘要不匹配: {name}")
            normalized.append(
                {
                    "name": skill.name,
                    "version": skill.version,
                    "digest": skill.digest,
                    "enabled": bool(raw.get("enabled", True)),
                }
            )
            seen.add(name)
        return normalized

    def _scan(self) -> dict[str, BuiltinSkill]:
        root = self._root
        if not root.is_absolute():
            root = Path.cwd() / root
        if not root.exists():
            return {}
        if not root.is_dir():
            raise FailException("BUILTIN_SKILLS_ROOT 必须是目录")
        catalog: dict[str, BuiltinSkill] = {}
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            if not _SKILL_NAME.fullmatch(child.name):
                raise FailException(f"内置 Skill 目录名非法: {child.name}")
            skill_file = child / "SKILL.md"
            if not skill_file.is_file() or skill_file.is_symlink():
                raise FailException(f"内置 Skill 缺少 SKILL.md: {child.name}")
            skill_md = skill_file.read_text(encoding="utf-8")
            metadata = _parse_frontmatter(skill_md)
            name = str(metadata.get("name", "")).strip()
            if name != child.name:
                raise FailException(f"Skill name 必须与目录名一致: {child.name}")
            description = str(metadata.get("description", "")).strip()
            if not description:
                raise FailException(f"Skill description 不能为空: {child.name}")
            _validate_package_tree(child)
            version = str(
                ((metadata.get("metadata") or {}).get("my_dify") or {}).get(
                    "version",
                    "1.0.0",
                )
            )
            my_dify_metadata = (metadata.get("metadata") or {}).get("my_dify") or {}
            category = str(my_dify_metadata.get("category") or "general").strip()
            execution_mode = str(
                my_dify_metadata.get("execution_mode") or "inline"
            ).strip()
            requires = _normalize_string_list(my_dify_metadata.get("requires"))
            allowed_tools = _normalize_string_list(metadata.get("allowed-tools"))
            catalog[name] = BuiltinSkill(
                name=name,
                description=description,
                version=version,
                digest=_digest_tree(child),
                skill_md=skill_md,
                package_root=child.resolve(),
                package_files=MappingProxyType(_read_text_package(child)),
                category=category,
                execution_mode=execution_mode,
                requires=requires,
                allowed_tools=allowed_tools,
            )
        return catalog


def _parse_frontmatter(skill_md: str) -> dict[str, Any]:
    lines = skill_md.splitlines()
    if not lines or lines[0].strip() != "---":
        raise FailException("SKILL.md 必须包含 YAML frontmatter")
    try:
        end = lines[1:].index("---") + 1
    except ValueError as exc:
        raise FailException("SKILL.md frontmatter 未闭合") from exc
    data = yaml.safe_load("\n".join(lines[1:end])) or {}
    if not isinstance(data, dict):
        raise FailException("SKILL.md frontmatter 必须是对象")
    return data


def _normalize_string_list(value: Any) -> tuple[str, ...]:
    """兼容规范中的空格字符串和内置模板常用的 YAML 列表。"""
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item for item in value.split() if item)
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),) if str(value).strip() else ()


def _digest_tree(root: Path) -> str:
    hasher = hashlib.sha256()
    for file in _iter_package_files(root):
        if file.is_symlink():
            raise FailException(f"内置 Skill 不允许符号链接: {file}")
        relative = file.relative_to(root).as_posix()
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(file.read_bytes())
        hasher.update(b"\0")
    return f"sha256:{hasher.hexdigest()}"


def _validate_package_tree(root: Path) -> None:
    """拒绝符号链接、特殊文件和超限的内置包。"""
    file_count = 0
    total_bytes = 0
    for entry in root.rglob("*"):
        if _is_runtime_cache(entry, root):
            continue
        if entry.is_symlink():
            raise FailException(f"内置 Skill 不允许符号链接: {entry}")
        if entry.is_dir():
            continue
        if not entry.is_file():
            raise FailException(f"内置 Skill 只允许普通文件: {entry}")
        file_count += 1
        total_bytes += entry.stat().st_size
        if file_count > _MAX_PACKAGE_FILES or total_bytes > _MAX_PACKAGE_BYTES:
            raise FailException(f"内置 Skill 包超过文件或字节上限: {root.name}")


def _read_text_package(root: Path) -> dict[str, str]:
    """加载安全的 UTF-8 包文件投影。"""
    files: dict[str, str] = {}
    for file in _iter_package_files(root):
        if file.is_symlink():
            raise FailException(f"内置 Skill 不允许符号链接: {file}")
        try:
            files[file.relative_to(root).as_posix()] = file.read_text(
                encoding="utf-8"
            )
        except UnicodeDecodeError as exc:
            raise FailException(f"内置 Skill 文件必须是 UTF-8 文本: {file}") from exc
    return files


def _iter_package_files(root: Path) -> list[Path]:
    """返回排除运行时缓存的有序源文件。"""
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not _is_runtime_cache(path, root)
    )


def _is_runtime_cache(path: Path, root: Path) -> bool:
    """判断是否为不发布的 Python 缓存产物。"""
    relative = path.relative_to(root)
    return "__pycache__" in relative.parts or path.suffix == ".pyc"


__all__ = ["BuiltinSkill", "SkillService"]
