"""平台 SubAgent 配置到 DeepAgents 原生规格的适配。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from deepagents.middleware.filesystem import FilesystemPermission
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from internal.exception import FailException, SubAgentConfigError


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
    }
)


_STATELESS_CONTRACT = (
    "你通过同步委派执行一个独立任务。你看不到父会话历史，只能使用当前任务描述、"
    "获准工具和可见文件。不要猜测未提供的上下文；信息不足时在最终回复中明确指出。"
    "只把完整最终结果返回给主 Agent，由主 Agent负责综合并回复用户。"
)


def validate_agent_tools(
    tools: Sequence[BaseTool],
    *,
    context: str = "Agent",
) -> list[BaseTool]:
    """复制并校验一组工具，防止名称冲突或覆盖 DeepAgents 内置工具。"""
    resolved = list(tools)
    names = [tool.name for tool in resolved]
    if len(names) != len(set(names)):
        raise FailException(f"{context} 工具名称冲突，无法安全装配运行时工具")

    reserved = sorted(set(names) & _DEEPAGENTS_RESERVED_TOOL_NAMES)
    if reserved:
        raise FailException(
            f"{context} 工具名称与 DeepAgents 内置工具冲突: "
            + ", ".join(reserved)
        )
    return resolved


def _validate_subagent_tools(
    tools: Sequence[BaseTool],
    *,
    name: str,
) -> list[BaseTool]:
    """把公共工具规则转换为 SubAgent 配置异常。"""
    try:
        return validate_agent_tools(tools, context=f"subagent {name!r}")
    except FailException as exc:
        raise SubAgentConfigError(str(exc)) from exc


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
    """将 Service 已规范化的同步 SubAgent 转为原生规格。"""
    # 1.这里只接收同步 SubAgent，异步规格由独立转换函数处理。
    normalized = [
        dict(item) for item in (subagents or []) if item.get("mode", "sync") == "sync"
    ]
    base_tool_map = {tool.name: tool for tool in available_tools}
    scoped_skills = skill_sources_by_agent or {}
    tool_overrides = subagent_tools_by_name or {}
    model_overrides = subagent_models_by_name or {}
    specs: list[dict[str, Any]] = []
    for config in normalized:
        name = config["name"]
        # 2.解析每个 SubAgent 被授权的工具和 Skill 路径。
        requested_tools = config.get("tools", [])
        if name in tool_overrides:
            resolved_tools = _validate_subagent_tools(
                tool_overrides[name],
                name=name,
            )
        else:
            missing_tools = sorted(set(requested_tools) - set(base_tool_map))
            if missing_tools:
                raise SubAgentConfigError(
                    f"subagent {name!r} 引用了未授权工具: {', '.join(missing_tools)}"
                )
            resolved_tools = _validate_subagent_tools(
                [base_tool_map[item] for item in requested_tools],
                name=name,
            )
        sources = list(scoped_skills.get(name, ()))
        if config.get("skills", []) and not sources:
            raise SubAgentConfigError(f"subagent {name!r} 的 Skills 尚未完成运行时授权")

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
        # 3.补充文件权限、模型覆盖和原生 Skill 配置。
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
    """将 Service 已规范化的异步 SubAgent 转为原生规格。"""
    # 1.筛选异步委派配置，并保留远程图所需的最小字段。
    specs: list[dict[str, Any]] = []
    for config in subagents or []:
        if config.get("mode", "sync") != "async":
            continue
        # graph_id 是 Agent Protocol 服务上的 graph key/assistant ID；
        # url 仅在跨部署 HTTP 调用时提供，省略时由 SDK 使用 ASGI transport。
        graph_id = config.get("graph_id")
        if not isinstance(graph_id, str) or not graph_id.strip():
            raise SubAgentConfigError(
                f"subagent {config.get('name', '<unknown>')!r} 缺少 graph_id"
            )
        spec: dict[str, Any] = {
            "name": config["name"],
            "description": config["description"],
            "graph_id": graph_id,
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
    "build_async_subagent_specs",
    "build_subagent_specs",
    "validate_agent_tools",
]
