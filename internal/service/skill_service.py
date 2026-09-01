"""内置 Skill 目录和 AppConfig 绑定校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from pydantic import ValidationError

from config.config import Config
from internal.exception import FailException, NotFoundException
from internal.schema.skill_schema import (
    BuiltinSkillManifestReq,
    SkillBindingListReq,
    SkillBindingReq,
    normalize_manifest_string_list,
)
from .base_service import BaseService


_SKILL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_MAX_PACKAGE_FILES = 256
_MAX_PACKAGE_BYTES = 20 * 1024 * 1024

# 每个 Skill 只声明一个最终分类；该表仅提供展示名称，不存在父子分类或归并关系。
SKILL_CATEGORY_LABELS = {
    "office": "办公效率",
    "content": "内容创作",
    "knowledge": "知识管理",
    "data": "数据分析",
    "education": "教育学习",
    "agent": "Agent 能力",
}


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

    def to_dict(self, *, enabled: bool | None = None) -> dict[str, Any]:
        """转换为 Skill 目录接口使用的字典。"""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "digest": self.digest,
            "category": self.category,
            "category_name": SKILL_CATEGORY_LABELS[self.category],
            "execution_mode": self.execution_mode,
            "requires": list(self.requires),
            "allowed_tools": list(self.allowed_tools),
            "enabled": self.enabled if enabled is None else enabled,
        }


class SkillService(BaseService):
    """读取内置 Skill 目录并校验 AppConfig 的绑定。

    Skill 内容来自版本化内置目录，应用配置只保存绑定引用。内置包在同一
    版本内发生修订时，旧摘要会在读取时自动刷新；版本变化仍要求显式更新。
    虚拟路径和 Backend 路由的创建由 Agent Core 的 SkillContextAssembler 完成。
    """

    def __init__(self, *args, **kwargs) -> None:
        """初始化内置 Skill 根目录，不在服务启动时加载全部内容。"""
        del args, kwargs
        settings = Config()
        self._root = Path(settings.BUILTIN_SKILLS_ROOT)

    def list_catalog(self) -> list[BuiltinSkill]:
        """读取当前版本的全部内置 Skill，并按名称排序。"""
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
        """返回 Skill 目录及其在当前应用中的启用状态。"""
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
        bindings: Sequence[SkillBindingReq | dict[str, Any]] | None = None,
    ) -> list[BuiltinSkill]:
        """返回当前应用启用且版本校验通过的 Skill。"""
        del account_id, app_id
        # 1.扫描当前版本的内置 Skill 目录。
        catalog = self._scan()
        selected: list[BuiltinSkill] = []
        # 2.只返回配置中启用且摘要校验通过的 Skill。
        for binding in self.normalize_bindings(bindings or []):
            if not binding["enabled"]:
                continue
            selected.append(catalog[binding["name"]])
        return selected

    def normalize_bindings(
        self,
        bindings: Sequence[SkillBindingReq | dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """解析 Skill 绑定，并刷新同版本内置包的过期摘要。"""
        try:
            parsed = SkillBindingListReq.model_validate(bindings or []).root
        except ValidationError as exc:
            raise FailException(f"Skill 绑定格式错误: {exc}") from exc

        catalog = self._scan()
        normalized: list[dict[str, Any]] = []
        for binding in parsed:
            name = binding.name
            if name not in catalog:
                raise FailException(f"Skill 不存在或未内置: {name}")
            skill = catalog[name]
            version = binding.version or skill.version
            if version != skill.version:
                raise FailException(f"Skill 版本不匹配: {name}")
            # 摘要是内置包内容的缓存指纹。平台升级同一 manifest 版本的
            # 指令或参考文件后，历史 AppConfig 仍可运行并获得新摘要。
            normalized.append(
                {
                    "name": skill.name,
                    "version": skill.version,
                    "digest": skill.digest,
                    "enabled": binding.enabled,
                }
            )
        return normalized

    def _scan(self) -> dict[str, BuiltinSkill]:
        """扫描并校验内置 Skill 目录，生成名称到元数据的映射。"""
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
            manifest = _parse_frontmatter(skill_md)
            name = manifest.name
            if name != child.name:
                raise FailException(f"Skill name 必须与目录名一致: {child.name}")
            description = manifest.description
            _validate_package_tree(child)
            metadata = manifest.metadata.my_dify
            requires = normalize_manifest_string_list(metadata.requires)
            allowed_tools = normalize_manifest_string_list(manifest.allowed_tools)
            catalog[name] = BuiltinSkill(
                name=name,
                description=description,
                version=metadata.version,
                digest=_digest_tree(child),
                skill_md=skill_md,
                package_root=child.resolve(),
                package_files=MappingProxyType(_read_text_package(child)),
                # 分类直接来自 manifest，前端通过同一层标签表显示中文名。
                category=metadata.category.strip().lower(),
                execution_mode=metadata.execution_mode,
                requires=requires,
                allowed_tools=allowed_tools,
            )
        return catalog


def _parse_frontmatter(skill_md: str) -> BuiltinSkillManifestReq:
    """解析 Skill Markdown 顶部的 YAML frontmatter。"""
    lines = skill_md.splitlines()
    if not lines or lines[0].strip() != "---":
        raise FailException("SKILL.md 必须包含 YAML frontmatter")
    try:
        end = lines[1:].index("---") + 1
    except ValueError as exc:
        raise FailException("SKILL.md frontmatter 未闭合") from exc
    try:
        data = yaml.safe_load("\n".join(lines[1:end])) or {}
        return BuiltinSkillManifestReq.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise FailException(f"SKILL.md frontmatter 格式错误: {exc}") from exc


def _digest_tree(root: Path) -> str:
    """计算 Skill 文件包的稳定摘要，用于版本绑定校验。"""
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
