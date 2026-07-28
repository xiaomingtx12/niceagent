"""Platform synchronous SubAgent schema mapped to native DeepAgents specs."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from deepagents.middleware.filesystem import FilesystemPermission
from langchain_core.tools import BaseTool


class SubAgentSpecError(ValueError):
    """Raised when persisted SubAgent configuration is unsafe or invalid."""


MAX_PLATFORM_SUBAGENTS = 8
MAX_SUBAGENT_DESCRIPTION_LENGTH = 500
MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH = 8000
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_FIELDS = {
    "name",
    "description",
    "system_prompt",
    "tools",
    "mode",
    "context_passing",
    "model",
    "skills",
}
_STATELESS_CONTRACT = (
    "你通过同步委派执行一个独立任务。你看不到父会话历史，只能使用当前任务描述、"
    "获准工具和可见文件。不要猜测未提供的上下文；信息不足时在最终回复中明确指出。"
    "只把完整最终结果返回给主 Agent，由主 Agent负责综合并回复用户。"
)


def validate_platform_subagents(
    subagents: list[dict[str, Any]] | dict[str, dict] | None,
) -> list[dict[str, Any]]:
    """Validate the persisted synchronous-team contract.

    ``mode=sync`` and ``context_passing=NONE`` remain in the normalized shape
    for draft/API compatibility. Other values fail closed: async execution and
    copied parent-history policies belong to later, separately designed phases.
    """
    if subagents is None:
        return []
    if isinstance(subagents, dict):
        converted = []
        for name, config in subagents.items():
            if not isinstance(config, dict):
                raise SubAgentSpecError(f"subagent {name!r} 配置必须是对象")
            converted.append({"name": name, **config})
        subagents = converted
    if not isinstance(subagents, list):
        raise SubAgentSpecError("subagents 必须是数组")
    if len(subagents) > MAX_PLATFORM_SUBAGENTS:
        raise SubAgentSpecError(f"subagents 数量不能超过 {MAX_PLATFORM_SUBAGENTS}")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(subagents):
        if not isinstance(raw, dict):
            raise SubAgentSpecError(f"subagents[{index}] 必须是对象")
        unexpected = set(raw) - _FIELDS
        if unexpected:
            raise SubAgentSpecError(
                f"subagents[{index}] 包含当前同步阶段不支持的字段: "
                f"{', '.join(sorted(unexpected))}"
            )

        name = raw.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise SubAgentSpecError(
                "sub-agent 名称必须以小写字母开头，只能包含小写字母、数字、_、-，最长 64 位"
            )
        if name in seen:
            raise SubAgentSpecError(f"sub-agent 名称重复: {name}")
        seen.add(name)

        mode = str(raw.get("mode", "sync")).lower()
        if mode != "sync":
            raise SubAgentSpecError(
                f"subagent {name!r} 当前仅支持 sync；异步执行将在独立阶段实现"
            )
        context_passing = str(raw.get("context_passing", "NONE")).upper()
        if context_passing != "NONE":
            raise SubAgentSpecError(
                f"subagent {name!r} 仅支持 context_passing=NONE；"
                "主 Agent必须在 task.description 中提供自包含任务说明"
            )

        description = raw.get("description")
        if not isinstance(description, str) or not description.strip():
            raise SubAgentSpecError(f"subagent {name!r} 缺 description")
        if len(description) > MAX_SUBAGENT_DESCRIPTION_LENGTH:
            raise SubAgentSpecError(
                f"subagent {name!r} description 不能超过 {MAX_SUBAGENT_DESCRIPTION_LENGTH} 个字符"
            )

        system_prompt = raw.get("system_prompt")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise SubAgentSpecError(f"subagent {name!r} 同步模式缺 system_prompt")
        if len(system_prompt) > MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH:
            raise SubAgentSpecError(
                f"subagent {name!r} system_prompt 不能超过 {MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH} 个字符"
            )

        tools = _normalize_name_list(raw.get("tools", []), name=name, field="tools")
        skills = _normalize_name_list(raw.get("skills", []), name=name, field="skills")
        item: dict[str, Any] = {
            "name": name,
            "description": description.strip(),
            "system_prompt": system_prompt.strip(),
            "mode": "sync",
            "context_passing": "NONE",
            "tools": tools,
            "skills": skills,
        }
        if raw.get("model"):
            item["model"] = str(raw["model"]).strip()
        normalized.append(item)
    return normalized


def build_subagent_specs(
    subagents: list[dict[str, Any]] | dict[str, dict] | None,
    *,
    available_tools: Sequence[BaseTool] = (),
    skill_sources_by_agent: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """Convert persisted records into native synchronous ``SubAgent`` specs."""
    normalized = validate_platform_subagents(subagents)
    tool_map = {tool.name: tool for tool in available_tools}
    scoped_skills = skill_sources_by_agent or {}
    specs: list[dict[str, Any]] = []
    for config in normalized:
        name = config["name"]
        requested_tools = config["tools"]
        missing_tools = sorted(set(requested_tools) - set(tool_map))
        if missing_tools:
            raise SubAgentSpecError(
                f"subagent {name!r} 引用了未授权工具: {', '.join(missing_tools)}"
            )
        sources = list(scoped_skills.get(name, ()))
        if config["skills"] and not sources:
            raise SubAgentSpecError(f"subagent {name!r} 的 Skills 尚未完成运行时授权")

        read_permissions = []
        if sources:
            read_permissions.append(
                FilesystemPermission(
                    operations=["read"],
                    paths=[f"{source}**" for source in sources],
                    mode="allow",
                )
            )
        read_permissions.append(
            FilesystemPermission(
                operations=["read"],
                paths=[
                    "/skills/**",
                    "/subagent_skills/**",
                    "/conversation_history/**",
                    "/memories/**",
                ],
                mode="deny",
            )
        )
        read_permissions.append(
            FilesystemPermission(
                operations=["read"],
                paths=["/**"],
                mode="deny",
            )
        )

        spec: dict[str, Any] = {
            "name": name,
            "description": (
                f"{config['description']} 委派时必须提供自包含任务说明，包括目标、输入、"
                "约束、期望输出和验收标准；不传父会话历史。"
            ),
            "system_prompt": f"{config['system_prompt']}\n\n{_STATELESS_CONTRACT}",
            # DeepAgents omitting this key means inherit every parent tool.
            # An explicit empty list is the platform's least-privilege default.
            "tools": [tool_map[item] for item in requested_tools],
            # A supplied permission list replaces parent rules. Read remains
            # allowed while every mutating filesystem operation is denied.
            "permissions": [
                *read_permissions,
                FilesystemPermission(
                    operations=["write"],
                    paths=["/**"],
                    mode="deny",
                )
            ],
        }
        if sources:
            spec["skills"] = sources
        if config.get("model"):
            spec["model"] = config["model"]
        specs.append(spec)
    return specs


def _normalize_name_list(value: Any, *, name: str, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        label = "工具" if field == "tools" else "Skill"
        raise SubAgentSpecError(f"subagent {name!r} {field} 必须是{label}名称数组")
    normalized = [item.strip() for item in value]
    if len(normalized) != len(set(normalized)):
        raise SubAgentSpecError(f"subagent {name!r} {field} 不允许重复")
    return normalized


__all__ = [
    "MAX_PLATFORM_SUBAGENTS",
    "SubAgentSpecError",
    "build_subagent_specs",
    "validate_platform_subagents",
]
