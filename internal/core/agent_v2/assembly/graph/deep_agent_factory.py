"""MyDify 的 DeepAgents 图装配入口。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Sequence

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from deepagents.middleware.filesystem import supports_execution
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ModelRetryMiddleware,
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver

from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.middleware.runtime_context_middleware import (
    RuntimeContextMiddleware,
)
from internal.core.agent_v2.middleware.review_middleware import ReviewMiddleware
from internal.core.agent_v2.middleware.trace_middleware import TraceMiddleware
from internal.core.agent_v2.middleware.reloading_memory_middleware import (
    ReloadingMemoryMiddleware,
)
from internal.core.agent_v2.assembly.graph.subagent_spec_adapter import (
    build_async_subagent_specs,
    build_subagent_specs,
)
from internal.core.agent_v2.assembly.observability.telemetry import AgentTelemetry


def build_deep_agent(
    *,
    llm: BaseChatModel,
    tools: Sequence[BaseTool],
    system_prompt: str,
    review_config: dict,
    max_model_calls: int = 10,
    max_tool_calls: int = 40,
    checkpointer: BaseCheckpointSaver | bool | None = None,
    backend: BackendProtocol | None = None,
    store: Any | None = None,
    permissions: Sequence[FilesystemPermission] | None = None,
    skill_sources: Sequence[str] = (),
    memory_sources: Sequence[str] = (),
    subagent_skill_sources: dict[str, Sequence[str]] | None = None,
    subagents: list[dict[str, Any]] | None = None,
    subagent_tools_by_name: Mapping[str, Sequence[BaseTool]] | None = None,
    subagent_models_by_name: Mapping[str, BaseChatModel | str] | None = None,
    telemetry: AgentTelemetry | None = None,
) -> Any:
    """装配 MyDify 运行时依赖并构造 DeepAgents 图。"""
    # 1.按业务边界组装事件、上下文、审查、重试和调用次数中间件。
    middleware = [
        RuntimeContextMiddleware(),
        ReviewMiddleware(review_config=review_config or {}),
        ModelRetryMiddleware(
            max_retries=2,
            retry_on=_is_transient_model_error,
            on_failure="error",
        ),
        ToolRetryMiddleware(
            max_retries=2,
            retry_on=_is_transient_tool_error,
            on_failure="error",
        ),
        ToolErrorMiddleware(on_error=_recoverable_tool_error),
        # Trace 位于重试和错误处理中间件内部，确保每次真实尝试都有
        # 一个 Span，同时保留已恢复工具错误的失败遥测。
        TraceMiddleware(telemetry),
        ModelCallLimitMiddleware(run_limit=max_model_calls, exit_behavior="end"),
        ToolCallLimitMiddleware(
            run_limit=max_tool_calls,
            exit_behavior="continue",
        ),
    ]
    if memory_sources:
        middleware.append(
            ReloadingMemoryMiddleware(
                backend=backend if backend is not None else StateBackend(),
                sources=list(memory_sources),
                add_cache_control=True,
            )
        )

    # 2.将已授权的同步、异步 SubAgent 配置转换为原生规格。
    # 持久化记录仅作业务适配，其余运行时能力由 DeepAgents 负责。
    subagent_specs = [
        *build_subagent_specs(
            subagents,
            available_tools=tools,
            skill_sources_by_agent=subagent_skill_sources,
            default_model=llm,
            subagent_tools_by_name=subagent_tools_by_name,
            subagent_models_by_name=subagent_models_by_name,
            sandbox_enabled=(backend is not None and supports_execution(backend)),
        ),
        *build_async_subagent_specs(subagents),
    ]

    # 3.把模型、工具、权限、持久化和上下文边界编译成运行图。
    return create_deep_agent(
        model=llm,
        tools=list(tools),
        system_prompt=system_prompt,
        middleware=middleware,
        subagents=subagent_specs,
        skills=list(skill_sources) or None,
        # 记忆由上面的刷新中间件装配，不能同时启用官方默认中间件，
        # 否则会在同一请求链中重复注入记忆提示词。
        memory=None,
        permissions=list(permissions or []),
        backend=backend,
        checkpointer=checkpointer,
        store=store,
        context_schema=AgentRunContext,
    )


def _is_transient_model_error(exc: Exception) -> bool:
    """只重试模型调用中的连接、超时、限流和服务端错误。"""
    return _is_transient_error(
        exc,
        name_tokens=("connection", "timeout", "rate", "temporar", "unavailable"),
    )


def _is_transient_tool_error(exc: Exception) -> bool:
    """只重试工具调用中的瞬时基础设施错误。"""
    return _is_transient_error(
        exc,
        name_tokens=("connection", "timeout", "rate", "temporar", "unavailable"),
    )


def _is_transient_error(
    exc: Exception,
    *,
    name_tokens: tuple[str, ...],
) -> bool:
    """判断异常是否属于模型或工具调用可安全重试的瞬时错误。"""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True

    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code == 429 or 500 <= status_code <= 599

    name = type(exc).__name__.lower()
    return any(token in name for token in name_tokens)


def _recoverable_tool_error(
    exc: Exception,
    request: Any,
) -> str | None:
    """把明确的参数错误交还模型，未知工具异常继续抛出。"""
    if isinstance(exc, (TypeError, ValueError, KeyError)):
        tool_call = getattr(request, "tool_call", {}) or {}
        tool_name = tool_call.get("name", "tool")
        return (
            f"Tool '{tool_name}' received invalid input. "
            "Correct the arguments and retry."
        )
    return None


__all__ = [
    "build_deep_agent",
]
