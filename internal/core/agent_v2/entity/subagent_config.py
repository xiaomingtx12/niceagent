"""SubAgent 编排配置及其领域约束。"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictStr,
    field_validator,
    model_validator,
)


MAX_SUBAGENT_DESCRIPTION_LENGTH = 500
MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH = 8000
MAX_SUBAGENT_GRAPH_ID_LENGTH = 255
MAX_SUBAGENT_HEADER_COUNT = 32
MAX_SUBAGENT_HEADER_NAME_LENGTH = 128
MAX_SUBAGENT_HEADER_VALUE_LENGTH = 2048
PUBLISHED_GRAPH_ID_PREFIX = "mydify:published:"

_SUBAGENT_NAME = re.compile(r'^(?!\.{1,2}$)[^\x00-\x1f/\\:*?"<>|]+$')


def build_published_graph_id(app_id: UUID | str) -> str:
    """构造 MyDify 已发布 Agent 的稳定 graph ID。"""
    return f"{PUBLISHED_GRAPH_ID_PREFIX}{app_id}"


class SubAgentConfig(BaseModel):
    """一个 SubAgent 的可保存编排配置。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )

    name: StrictStr = Field(min_length=1, max_length=64)
    description: StrictStr = Field(
        min_length=1,
        max_length=MAX_SUBAGENT_DESCRIPTION_LENGTH,
    )
    system_prompt: StrictStr = Field(
        default="",
        max_length=MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH,
    )
    tools: list[StrictStr] = Field(default_factory=list)
    skills: list[StrictStr] = Field(default_factory=list)
    mode: Literal["sync", "async"] = "sync"
    context_passing: Literal["NONE"] = "NONE"
    source: Literal["custom", "published"] = "custom"
    model: StrictStr | None = None
    graph_id: StrictStr | None = Field(
        default=None,
        max_length=MAX_SUBAGENT_GRAPH_ID_LENGTH,
        description="Agent Protocol 服务注册的 graph key 或 assistant ID",
    )
    url: StrictStr | None = Field(
        default=None,
        description="远程 Agent Protocol 服务地址；省略时使用同部署 ASGI transport",
    )
    headers: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    published_app_id: UUID | None = None

    @model_validator(mode="before")
    @classmethod
    def fill_published_graph_id(cls, value: Any) -> Any:
        """为已发布异步 Agent 自动补齐平台 graph ID。"""
        if not isinstance(value, dict):
            return value
        mode = str(value.get("mode", "sync")).lower()
        source = str(value.get("source", "custom")).lower()
        if (
            mode == "async"
            and source == "published"
            and value.get("published_app_id")
            and not value.get("graph_id")
        ):
            value = dict(value)
            value["graph_id"] = build_published_graph_id(value["published_app_id"])
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: StrictStr) -> StrictStr:
        """拒绝路径分隔符、控制字符和 Windows 保留字符。"""
        if _SUBAGENT_NAME.fullmatch(value) is None:
            raise ValueError("name 包含非法字符")
        return value

    @field_validator("mode", "source", mode="before")
    @classmethod
    def normalize_lowercase_enum(cls, value: Any) -> Any:
        """统一模式和来源的大小写。"""
        return value.lower() if isinstance(value, str) else value

    @field_validator("context_passing", mode="before")
    @classmethod
    def normalize_context_passing(cls, value: Any) -> Any:
        """统一上下文传递策略的大小写。"""
        return value.upper() if isinstance(value, str) else value

    @field_validator("tools", "skills")
    @classmethod
    def validate_name_list(cls, value: list[str]) -> list[str]:
        """确保工具和 Skill 名称非空且不重复。"""
        if any(not item for item in value):
            raise ValueError("列表中不允许空名称")
        if len(value) != len(set(value)):
            raise ValueError("列表中不允许重复名称")
        return value

    @field_validator("model")
    @classmethod
    def validate_model_reference(
        cls,
        value: StrictStr | None,
    ) -> StrictStr | None:
        """校验自定义同步 SubAgent 的 provider/model 标识。"""
        if value is None:
            return None
        parts = value.split("/")
        if len(parts) != 2 or any(not part for part in parts):
            raise ValueError("model 必须是 provider/model 格式")
        return "/".join(parts)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: StrictStr | None) -> StrictStr | None:
        """校验远程 SubAgent 地址。"""
        if value is None or not value.strip():
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url 必须是 http/https 地址")
        return value

    @field_validator("headers")
    @classmethod
    def validate_headers(
        cls,
        value: dict[StrictStr, StrictStr],
    ) -> dict[StrictStr, StrictStr]:
        """校验远程 SubAgent 请求头的数量和长度。"""
        if len(value) > MAX_SUBAGENT_HEADER_COUNT:
            raise ValueError(f"headers 不能超过 {MAX_SUBAGENT_HEADER_COUNT} 项")
        for key, header_value in value.items():
            if not key or len(key) > MAX_SUBAGENT_HEADER_NAME_LENGTH:
                raise ValueError("headers 名称必须是非空短字符串")
            if len(header_value) > MAX_SUBAGENT_HEADER_VALUE_LENGTH:
                raise ValueError("headers 值过长")
        return value

    @model_validator(mode="after")
    def validate_mode_constraints(self) -> "SubAgentConfig":
        """校验同步、异步和发布 Agent 之间的字段约束。"""
        if self.mode == "async":
            if self.source == "published":
                if self.published_app_id is None:
                    raise ValueError("异步发布 SubAgent 缺少 published_app_id")
                expected_graph_id = build_published_graph_id(self.published_app_id)
                if self.graph_id != expected_graph_id:
                    raise ValueError("异步发布 SubAgent 的 graph_id 与发布应用不匹配")
            elif not self.graph_id:
                raise ValueError("异步 SubAgent 缺少 graph_id")
            if self.system_prompt or self.tools or self.skills or self.model:
                raise ValueError(
                    "异步 SubAgent 不支持 system_prompt、tools、skills 或 model"
                )
        elif self.source == "custom":
            if not self.system_prompt:
                raise ValueError("同步自定义 SubAgent 缺少 system_prompt")
        elif self.published_app_id is None:
            raise ValueError("发布 SubAgent 缺少 published_app_id")
        return self


class SubAgentConfigList(RootModel[list[SubAgentConfig]]):
    """SubAgent 编排配置列表及其列表级约束。"""

    @model_validator(mode="after")
    def validate_unique_names(self) -> "SubAgentConfigList":
        """拒绝同一应用中的重复 SubAgent 名称。"""
        names = [config.name for config in self.root]
        if len(names) != len(set(names)):
            raise ValueError("SubAgent 名称不能重复")
        return self


__all__ = [
    "PUBLISHED_GRAPH_ID_PREFIX",
    "SubAgentConfig",
    "SubAgentConfigList",
    "build_published_graph_id",
]
