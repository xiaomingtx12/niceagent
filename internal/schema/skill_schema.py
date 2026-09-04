"""Skill 清单、应用绑定和查询参数的结构化校验。"""

from __future__ import annotations

from typing import Any, Literal

from marshmallow import Schema, fields, pre_dump
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictBool,
    StrictStr,
    model_validator,
)

from internal.entity import BuiltinSkill


SKILL_CATEGORY_LABELS = {
    "office": "办公效率",
    "content": "内容创作",
    "knowledge": "知识管理",
    "data": "数据分析",
    "education": "教育学习",
    "agent": "Agent 能力",
}


class SkillBindingReq(BaseModel):
    """应用配置中保存的一个 Skill 版本绑定。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: StrictStr = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    version: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    digest: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    enabled: StrictBool = True


class SkillBindingListReq(RootModel[list[SkillBindingReq]]):
    """应用的 Skill 绑定列表及其唯一性约束。"""

    @model_validator(mode="after")
    def validate_unique_names(self) -> "SkillBindingListReq":
        names = [binding.name for binding in self.root]
        if len(names) != len(set(names)):
            raise ValueError("Skill 绑定不能重复")
        return self


class BuiltinSkillManifestMetadataReq(BaseModel):
    """SKILL.md 中 my_dify 扩展元数据。"""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    version: StrictStr = Field(default="1.0.0", min_length=1, max_length=64)
    category: Literal[
        "office", "content", "knowledge", "data", "education", "agent"
    ] = "agent"
    execution_mode: Literal["inline", "sandbox"] = "inline"
    requires: StrictStr | list[StrictStr] = Field(default_factory=list)


class BuiltinSkillManifestEnvelopeReq(BaseModel):
    """SKILL.md metadata 字段的扩展容器。"""

    model_config = ConfigDict(extra="allow")

    my_dify: BuiltinSkillManifestMetadataReq = Field(
        default_factory=BuiltinSkillManifestMetadataReq
    )


class BuiltinSkillManifestReq(BaseModel):
    """SKILL.md frontmatter 的可信结构。"""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    name: StrictStr = Field(min_length=1, max_length=64)
    description: StrictStr = Field(min_length=1)
    metadata: BuiltinSkillManifestEnvelopeReq = Field(
        default_factory=BuiltinSkillManifestEnvelopeReq
    )
    allowed_tools: StrictStr | list[StrictStr] = Field(
        default_factory=list,
        alias="allowed-tools",
    )


def normalize_manifest_string_list(value: Any) -> tuple[str, ...]:
    """把 Skill 规范允许的字符串或字符串数组统一成元组。"""
    if isinstance(value, str):
        return tuple(part for part in value.split() if part)
    return tuple(value)


class _BuiltinSkillFields(Schema):
    """内置 Skill 响应共享字段。"""

    name = fields.String(required=True)
    description = fields.String(required=True)
    version = fields.String(required=True)
    digest = fields.String(required=True)
    category = fields.String(required=True)
    category_name = fields.String(required=True)
    execution_mode = fields.String(required=True)
    requires = fields.List(fields.String(), required=True)
    allowed_tools = fields.List(fields.String(), required=True)
    enabled = fields.Boolean(required=True)

    @staticmethod
    def _project(skill: BuiltinSkill) -> dict[str, Any]:
        """只投影 HTTP 响应允许公开的 Skill 字段。"""
        return {
            "name": skill.name,
            "description": skill.description,
            "version": skill.version,
            "digest": skill.digest,
            "category": skill.category,
            "category_name": SKILL_CATEGORY_LABELS[skill.category],
            "execution_mode": skill.execution_mode,
            "requires": list(skill.requires),
            "allowed_tools": list(skill.allowed_tools),
            "enabled": skill.enabled,
        }


class BuiltinSkillResp(_BuiltinSkillFields):
    """内置 Skill 目录项响应。"""

    @pre_dump
    def project_skill(self, skill: BuiltinSkill, **kwargs):
        """将领域对象显式转换为公开响应。"""
        return self._project(skill)


class BuiltinSkillDetailResp(_BuiltinSkillFields):
    """包含 SKILL.md 正文的内置 Skill 详情响应。"""

    content = fields.String(required=True)

    @pre_dump
    def project_skill(self, skill: BuiltinSkill, **kwargs):
        """在目录字段基础上追加公开的 Skill 正文。"""
        data = self._project(skill)
        data["content"] = skill.skill_md
        return data


class ListBuiltinSkillsResp(Schema):
    """内置 Skill 目录列表响应。"""

    items = fields.List(fields.Nested(BuiltinSkillResp), required=True)


__all__ = [
    "BuiltinSkillManifestReq",
    "BuiltinSkillDetailResp",
    "BuiltinSkillResp",
    "ListBuiltinSkillsResp",
    "SkillBindingListReq",
    "SkillBindingReq",
    "normalize_manifest_string_list",
]
