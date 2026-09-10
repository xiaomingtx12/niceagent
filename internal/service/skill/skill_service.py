"""内置 Skill Catalog 和应用绑定业务。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from injector import inject
from pydantic import ValidationError

from internal.entity import BuiltinSkill
from internal.exception import FailException, NotFoundException, ValidateErrorException
from internal.repository import BuiltinSkillRepository, BuiltinSkillRepositoryError
from internal.schema.skill.skill_schema import SkillBindingListReq, SkillBindingReq


@inject
@dataclass
class SkillService:
    """校验应用的 Skill 绑定，并从 Catalog 解析运行时 Skill。"""

    repository: BuiltinSkillRepository

    def list_catalog(
        self,
        bindings: Sequence[SkillBindingReq | Mapping[str, Any]] | None = None,
    ) -> list[BuiltinSkill]:
        """返回 Catalog；传入绑定时附带当前应用的启用状态。"""
        catalog = self._load_catalog()
        if bindings is None:
            return sorted(catalog.values(), key=lambda item: item.name)

        normalized = self._normalize_bindings(bindings, catalog)
        enabled_names = {
            item["name"] for item in normalized if bool(item["enabled"])
        }
        return [
            replace(skill, enabled=skill.name in enabled_names)
            for skill in sorted(catalog.values(), key=lambda item: item.name)
        ]

    def get_catalog_skill(self, name: str) -> BuiltinSkill:
        """按名称返回一个内置 Skill，不存在时抛出统一业务异常。"""
        catalog = self._load_catalog()
        skill = catalog.get(name)
        if skill is None:
            raise NotFoundException("Skill 模板不存在")
        return skill

    def normalize_bindings(
        self,
        bindings: Sequence[SkillBindingReq | Mapping[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """校验持久化绑定，并刷新同版本内置包的摘要。"""
        catalog = self._load_catalog()
        return self._normalize_bindings(bindings or (), catalog)

    def resolve_enabled_skills(
        self,
        bindings: Sequence[SkillBindingReq | Mapping[str, Any]] | None,
    ) -> list[BuiltinSkill]:
        """解析当前应用已启用且版本匹配的运行时 Skill。"""
        catalog = self._load_catalog()
        normalized = self._normalize_bindings(bindings or (), catalog)
        return [
            catalog[item["name"]]
            for item in normalized
            if bool(item["enabled"])
        ]

    def _load_catalog(self) -> Mapping[str, BuiltinSkill]:
        """在业务 seam 将文件系统故障转换为项目统一失败异常。"""
        try:
            return self.repository.load_catalog()
        except BuiltinSkillRepositoryError as exc:
            raise FailException(str(exc)) from exc

    @staticmethod
    def _normalize_bindings(
        bindings: Sequence[SkillBindingReq | Mapping[str, Any]],
        catalog: Mapping[str, BuiltinSkill],
    ) -> list[dict[str, Any]]:
        """使用同一份 Catalog 快照完成结构和业务规则校验。"""
        try:
            parsed = SkillBindingListReq.model_validate(bindings).root
        except ValidationError as exc:
            raise ValidateErrorException(f"Skill 绑定格式错误: {exc}") from exc

        normalized: list[dict[str, Any]] = []
        for binding in parsed:
            skill = catalog.get(binding.name)
            if skill is None:
                raise ValidateErrorException(
                    f"Skill 不存在或未内置: {binding.name}"
                )
            version = binding.version or skill.version
            if version != skill.version:
                raise ValidateErrorException(f"Skill 版本不匹配: {binding.name}")
            normalized.append(
                {
                    "name": skill.name,
                    "version": skill.version,
                    "digest": skill.digest,
                    "enabled": binding.enabled,
                }
            )
        return normalized


__all__ = ["SkillService"]
