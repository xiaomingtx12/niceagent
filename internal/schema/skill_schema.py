"""Skill 清单和应用绑定的结构化校验。"""

from __future__ import annotations

from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictBool,
    StrictStr,
    model_validator,
)


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
    category: StrictStr = Field(default="general", min_length=1, max_length=64)
    execution_mode: StrictStr = Field(default="inline", min_length=1, max_length=64)
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


__all__ = [
    "BuiltinSkillManifestReq",
    "SkillBindingListReq",
    "SkillBindingReq",
    "normalize_manifest_string_list",
]
