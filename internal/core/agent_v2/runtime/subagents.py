"""平台 SubAgent 配置到 DeepAgents 原生规格的适配。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from deepagents.middleware.filesystem import FilesystemPermission
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool


class SubAgentSpecError(ValueError):
    """持久化 SubAgent 配置不安全或无效时抛出。"""


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
    "graph_id",
    "url",
    "headers",
    "source",
    "published_app_id",
}
_STATELESS_CONTRACT = (
    "你通过同步委派执行一个独立任务。你看不到父会话历史，只能使用当前任务描述、"
    "获准工具和可见文件。不要猜测未提供的上下文；信息不足时在最终回复中明确指出。"
    "只把完整最终结果返回给主 Agent，由主 Agent负责综合并回复用户。"
)


def validate_platform_subagents(
    subagents: list[dict[str, Any]] | dict[str, dict] | None,
) -> list[dict[str, Any]]:
    """校验持久化的同步、异步和发布 Agent 团队配置。"""
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

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(subagents):
        if not isinstance(raw, dict):
            raise SubAgentSpecError(f"subagents[{index}] 必须是对象")
        unexpected = set(raw) - _FIELDS
        if unexpected:
            raise SubAgentSpecError(
                f"subagents[{index}] 包含不支持的字段: "
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
        if mode not in {"sync", "async"}:
            raise SubAgentSpecError(
                f"subagent {name!r} mode 必须是 sync 或 async"
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
                f"subagent {name!r} description 不能超过 "
                f"{MAX_SUBAGENT_DESCRIPTION_LENGTH} 个字符"
            )

        source = str(raw.get("source", "custom")).lower()
        if source not in {"custom", "published"}:
            raise SubAgentSpecError(
                f"subagent {name!r} source 必须是 custom 或 published"
            )
        if mode == "async" and source != "custom":
            raise SubAgentSpecError(
                f"subagent {name!r} 发布 Agent 当前只能以 sync 模式引用"
            )

        system_prompt = raw.get("system_prompt", "")
        if not isinstance(system_prompt, str):
            raise SubAgentSpecError(f"subagent {name!r} system_prompt 必须是字符串")
        if mode == "sync" and source == "custom" and not system_prompt.strip():
            raise SubAgentSpecError(f"subagent {name!r} 同步模式缺 system_prompt")
        if len(system_prompt) > MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH:
            raise SubAgentSpecError(
                f"subagent {name!r} system_prompt 不能超过 "
                f"{MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH} 个字符"
            )

        tools = _normalize_name_list(raw.get("tools", []), name=name, field="tools")
        skills = _normalize_name_list(raw.get("skills", []), name=name, field="skills")
        item: dict[str, Any] = {
            "name": name,
            "description": description.strip(),
            "mode": mode,
            "context_passing": "NONE",
            "tools": tools,
            "skills": skills,
        }
        if mode == "sync":
            item["system_prompt"] = system_prompt.strip()
            if source == "published":
                item["source"] = "published"
                item["published_app_id"] = _normalize_uuid(
                    raw.get("published_app_id"),
                    name=name,
                    field="published_app_id",
                )
            elif raw.get("model"):
                item["model"] = str(raw["model"]).strip()
        else:
            graph_id = raw.get("graph_id")
            if not isinstance(graph_id, str) or not graph_id.strip():
                raise SubAgentSpecError(
                    f"subagent {name!r} 异步模式缺 graph_id"
                )
            if len(graph_id.strip()) > 255:
                raise SubAgentSpecError(
                    f"subagent {name!r} graph_id 不能超过 255 个字符"
                )
            item["graph_id"] = graph_id.strip()
            url = raw.get("url")
            if not isinstance(url, str) or not url.strip():
                raise SubAgentSpecError(
                    f"subagent {name!r} 异步模式缺 url；当前平台使用同步图驱动，"
                    "必须配置 Agent Protocol 服务地址"
                )
            parsed = urlparse(url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise SubAgentSpecError(
                    f"subagent {name!r} url 必须是 http/https 地址"
                )
            item["url"] = url.strip()
            if system_prompt.strip():
                raise SubAgentSpecError(
                    f"subagent {name!r} 异步模式不支持 system_prompt"
                )
            if tools or skills:
                raise SubAgentSpecError(
                    f"subagent {name!r} 异步模式不支持本地 tools 或 skills"
                )
            if raw.get("model"):
                raise SubAgentSpecError(
                    f"subagent {name!r} 异步模式不支持本地 model"
                )
            item["headers"] = _normalize_headers(
                raw.get("headers", {}),
                name=name,
            )
        normalized.append(item)
    return normalized


def build_subagent_specs(
    subagents: Sequence[Mapping[str, Any]] | None,
    *,
    available_tools: Sequence[BaseTool] = (),
    skill_sources_by_agent: Mapping[str, Sequence[str]] | None = None,
    default_model: BaseChatModel | str | None = None,
    subagent_tools_by_name: Mapping[str, Sequence[BaseTool]] | None = None,
    subagent_models_by_name: Mapping[str, BaseChatModel | str] | None = None,
    sandbox_enabled: bool = False,
) -> list[dict[str, Any]]:
    """将已标准化的配置转换为原生同步 SubAgent 规格。

    配置校验只在 AppConfig 保存边界执行一次。这里不再重复校验业务
    字段，只负责把已授权的工具、Skill 和文件权限映射为 DeepAgents
    原生 ``SubAgent`` 规格。
    """
    normalized = [
        item
        for item in (subagents or [])
        if item.get("mode", "sync") == "sync"
    ]
    base_tool_map = {tool.name: tool for tool in available_tools}
    scoped_skills = skill_sources_by_agent or {}
    tool_overrides = subagent_tools_by_name or {}
    model_overrides = subagent_models_by_name or {}
    specs: list[dict[str, Any]] = []
    for config in normalized:
        name = config["name"]
        requested_tools = config.get("tools", [])
        if name in tool_overrides:
            resolved_tools = list(tool_overrides[name])
            resolved_names = [tool.name for tool in resolved_tools]
            if len(resolved_names) != len(set(resolved_names)):
                raise SubAgentSpecError(
                    f"subagent {name!r} 运行时工具名称重复"
                )
        else:
            missing_tools = sorted(set(requested_tools) - set(base_tool_map))
            if missing_tools:
                raise SubAgentSpecError(
                    f"subagent {name!r} 引用了未授权工具: {', '.join(missing_tools)}"
                )
            resolved_tools = [base_tool_map[item] for item in requested_tools]
        sources = list(scoped_skills.get(name, ()))
        if config.get("skills", []) and not sources:
            raise SubAgentSpecError(f"subagent {name!r} 的 Skills 尚未完成运行时授权")

        spec: dict[str, Any] = {
            "name": name,
            "description": (
                f"{config['description']} 委派时必须提供自包含任务说明，包括目标、输入、"
                "约束、期望输出和验收标准；不传父会话历史。"
            ),
            "system_prompt": f"{config['system_prompt']}\n\n{_STATELESS_CONTRACT}",
            # 显式空列表避免继承父 Agent 的全部工具。
            "tools": resolved_tools,
        }
        if not sandbox_enabled:
            # 非 Sandbox 模式显式限制子 Agent 的文件范围；Sandbox 的隔离
            # 由原生 SandboxBackend 提供，不能再叠加工具级权限规则。
            spec["permissions"] = _build_subagent_permissions(sources)
        if sources:
            spec["skills"] = sources
        if name in model_overrides:
            spec["model"] = model_overrides[name]
        elif config.get("model"):
            spec["model"] = config["model"]
        elif default_model is not None:
            spec["model"] = default_model
        specs.append(spec)
    return specs


def build_async_subagent_specs(
    subagents: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """将已标准化的配置转换为 DeepAgents 原生 AsyncSubAgent 规格。"""
    specs: list[dict[str, Any]] = []
    for config in subagents or []:
        if config.get("mode", "sync") != "async":
            continue
        spec: dict[str, Any] = {
            "name": config["name"],
            "description": config["description"],
            "graph_id": config["graph_id"],
        }
        if config.get("url"):
            spec["url"] = config["url"]
        if config.get("headers"):
            spec["headers"] = dict(config["headers"])
        specs.append(spec)
    return specs


def _build_subagent_permissions(
    skill_sources: Sequence[str],
) -> list[FilesystemPermission]:
    """构造所有 Backend 模式一致的子 Agent 文件权限。

    DeepAgents 的子 Agent 默认复用父 Agent Backend。这里显式声明权限，
    避免 Sandbox 开启后突然获得长期记忆和其他隐藏路径的访问权，同时
    保留子 Agent 通过共享 Workspace 协作的能力。
    """
    permissions: list[FilesystemPermission] = []
    for source in skill_sources:
        normalized = source.rstrip("/") or "/"
        permissions.append(
            FilesystemPermission(
                operations=["read"],
                paths=[normalized, f"{normalized}/**"],
                mode="allow",
            )
        )

    permissions.append(
        FilesystemPermission(
            operations=["read", "write"],
            paths=[
                "/workspace",
                "/workspace/**",
            ],
            mode="allow",
        )
    )
    permissions.extend(
        [
            FilesystemPermission(
                operations=["read"],
                paths=["/", "/**"],
                mode="deny",
            ),
            FilesystemPermission(
                operations=["write"],
                paths=["/", "/**"],
                mode="deny",
            ),
        ]
    )
    return permissions


def _normalize_name_list(value: Any, *, name: str, field: str) -> list[str]:
    """校验并规范唯一的工具或 Skill 名称列表。"""
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        label = "工具" if field == "tools" else "Skill"
        raise SubAgentSpecError(f"subagent {name!r} {field} 必须是{label}名称数组")
    normalized = [item.strip() for item in value]
    if len(normalized) != len(set(normalized)):
        raise SubAgentSpecError(f"subagent {name!r} {field} 不允许重复")
    return normalized


def _normalize_uuid(value: Any, *, name: str, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise SubAgentSpecError(
            f"subagent {name!r} {field} 必须是 UUID"
        ) from exc


def _normalize_headers(value: Any, *, name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise SubAgentSpecError(f"subagent {name!r} headers 必须是对象")
    if len(value) > 32:
        raise SubAgentSpecError(f"subagent {name!r} headers 不能超过 32 项")
    headers: dict[str, str] = {}
    for key, raw_value in value.items():
        if (
            not isinstance(key, str)
            or not key.strip()
            or len(key.strip()) > 128
            or not isinstance(raw_value, str)
            or len(raw_value) > 2048
        ):
            raise SubAgentSpecError(
                f"subagent {name!r} headers 必须是非空字符串键值"
            )
        headers[key.strip()] = raw_value
    return headers


__all__ = [
    "SubAgentSpecError",
    "build_async_subagent_specs",
    "build_subagent_specs",
    "validate_platform_subagents",
]
