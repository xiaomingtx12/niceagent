"""平台 SubAgent 配置到 DeepAgents 原生规格的适配。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from deepagents.middleware.async_subagents import AsyncSubAgent
from deepagents.middleware.filesystem import FilesystemPermission
from deepagents.middleware.subagents import SubAgent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from internal.core.agent_v2.entity.subagent_config import SubAgentConfig
from internal.core.agent_v2.entity.agent_config import SubAgentRuntimeBinding
from internal.core.agent_v2.middleware.tool_call_validation_middleware import (
    ToolCallValidationMiddleware,
)

_DEEPAGENTS_RESERVED_TOOL_NAMES = frozenset(
    {
        "task",
        "write_todos",
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "execute",
        "search_memory",
        "update_memory",
        "delete_memory_file",
        "ask_user",
    }
)


def validate_agent_tools(
    tools: Sequence[BaseTool],
    *,
    context: str = "Agent",
) -> list[BaseTool]:
    """复制并校验一组工具，防止名称冲突或覆盖 DeepAgents 内置工具。"""
    resolved = list(tools)
    names: set[str] = set()
    duplicates: set[str] = set()
    for tool in resolved:
        if not isinstance(tool.name, str) or not tool.name.strip():
            raise ValueError(f"{context} 工具名称不能为空")
        if tool.name in names:
            duplicates.add(tool.name)
        names.add(tool.name)
    if duplicates:
        raise ValueError(f"{context} 工具名称重复: {', '.join(sorted(duplicates))}")

    reserved = sorted(names & _DEEPAGENTS_RESERVED_TOOL_NAMES)
    if reserved:
        raise ValueError(
            f"{context} 工具名称与 DeepAgents 内置工具冲突: " + ", ".join(reserved)
        )
    return resolved


def build_subagent_specs(
    subagents: Sequence[SubAgentConfig],
    *,
    available_tools: Sequence[BaseTool] = (),
    skill_sources_by_agent: Mapping[str, Sequence[str]] | None = None,
    runtime_bindings: Mapping[str, SubAgentRuntimeBinding] | None = None,
) -> list[SubAgent | AsyncSubAgent]:
    """按配置顺序将平台 SubAgent 转为 DeepAgents 原生规格。"""
    base_tool_map = {tool.name: tool for tool in available_tools}
    scoped_skills = skill_sources_by_agent or {}
    bindings = runtime_bindings or {}
    _validate_runtime_names(subagents, scoped_skills, bindings)

    specs: list[SubAgent | AsyncSubAgent] = []
    for config in subagents:
        if config.mode == "async":
            specs.append(_build_async_spec(config))
            continue
        specs.append(
            _build_sync_spec(
                config,
                available_tools=base_tool_map,
                skill_sources=scoped_skills.get(config.name, ()),
                binding=bindings.get(config.name),
            )
        )
    return specs


def _build_sync_spec(
    config: SubAgentConfig,
    *,
    available_tools: Mapping[str, BaseTool],
    skill_sources: Sequence[str],
    binding: SubAgentRuntimeBinding | None,
) -> SubAgent:
    """解析一个同步 SubAgent 的授权资源并构造原生规格。"""
    tools = _resolve_subagent_tools(config, available_tools, binding)
    sources = list(skill_sources)
    if config.skills and not sources:
        raise ValueError(f"subagent {config.name!r} 的 Skills 尚未完成运行时授权")

    spec: SubAgent = {
        "name": config.name,
        "description": config.description,
        "system_prompt": config.system_prompt,
        # 显式空列表避免 DeepAgents 继承父 Agent 的全部工具。
        "tools": tools,
        # 子 Agent 权限替换父规则，避免继承主 Agent 的 Memory 和 Skill 范围。
        "permissions": _build_subagent_permissions(sources),
        "middleware": [ToolCallValidationMiddleware()],
    }
    if sources:
        spec["skills"] = sources
    if binding is not None and binding.model is not None:
        spec["model"] = binding.model
    elif config.model is not None:
        raise ValueError(f"subagent {config.name!r} 的模型尚未完成运行时解析")
    # 未覆盖 model 时由 DeepAgents 原生继承主 Agent 模型。
    return spec


def _build_async_spec(config: SubAgentConfig) -> AsyncSubAgent:
    """构造一个远程 Agent Protocol SubAgent 原生规格。"""
    graph_id = config.graph_id
    if graph_id is None:
        raise ValueError(f"subagent {config.name!r} 缺少 graph_id")
    spec: AsyncSubAgent = {
        "name": config.name,
        "description": config.description,
        "graph_id": graph_id,
    }
    if config.url is not None:
        spec["url"] = config.url
    if config.headers:
        spec["headers"] = dict(config.headers)
    return spec


def _resolve_subagent_tools(
    config: SubAgentConfig,
    available_tools: Mapping[str, BaseTool],
    binding: SubAgentRuntimeBinding | None,
) -> list[BaseTool]:
    """解析并核对一个同步 SubAgent 的工具授权。"""
    if binding is not None and binding.tools is not None:
        resolved = validate_agent_tools(
            binding.tools,
            context=f"subagent {config.name!r}",
        )
        configured_names = set(config.tools)
        resolved_names = {tool.name for tool in resolved}
        if resolved_names != configured_names:
            raise ValueError(f"subagent {config.name!r} 的运行时工具绑定与配置不一致")
        return resolved

    missing = sorted(set(config.tools) - set(available_tools))
    if missing:
        raise ValueError(
            f"subagent {config.name!r} 引用了未授权工具: {', '.join(missing)}"
        )
    return [available_tools[name] for name in config.tools]


def _validate_runtime_names(
    subagents: Sequence[SubAgentConfig],
    skill_sources: Mapping[str, Sequence[str]],
    bindings: Mapping[str, SubAgentRuntimeBinding],
) -> None:
    """拒绝无法对应到同步 SubAgent 的运行时资源，避免静默忽略装配错误。"""
    names: set[str] = set()
    duplicates: set[str] = set()
    for config in subagents:
        if config.name in names:
            duplicates.add(config.name)
        names.add(config.name)
    if duplicates:
        raise ValueError(f"SubAgent 名称重复: {', '.join(sorted(duplicates))}")

    sync_names = {config.name for config in subagents if config.mode == "sync"}
    unknown = sorted((set(skill_sources) | set(bindings)) - sync_names)
    if unknown:
        raise ValueError(f"存在未使用的 SubAgent 运行时资源: {', '.join(unknown)}")


def _build_subagent_permissions(
    skill_sources: Sequence[str],
) -> list[FilesystemPermission]:
    """构造所有 Backend 模式一致的子 Agent 文件权限。

    DeepAgents 的子 Agent 默认复用父 Backend 并继承父权限；这里用原生
    permissions 替换父规则，只开放已授权 Skill 和共享 Workspace。
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
                paths=["/"],
                mode="allow",
            ),
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


__all__ = [
    "SubAgentRuntimeBinding",
    "build_subagent_specs",
    "validate_agent_tools",
]
