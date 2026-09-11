"""MyDify 的 DeepAgents 图装配入口。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from deepagents import create_deep_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ModelRetryMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph
from openai import APIConnectionError

from internal.core.agent_v2.assembly.configuration.agent_system_prompt import (
    build_app_system_prompt,
)
from internal.core.agent_v2.execution.runtime_resources import (
    AgentRuntimeResources,
)
from internal.core.agent_v2.assembly.graph.subagent_spec_adapter import (
    build_subagent_specs,
    validate_agent_tools,
)
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.entity.agent_config import SubAgentDefinition
from internal.core.agent_v2.middleware.reloading_memory_middleware import ReloadingMemoryMiddleware
from internal.core.agent_v2.middleware.context_window_middleware import ContextWindowMiddleware
from internal.core.agent_v2.middleware.review_middleware import ReviewMiddleware
from internal.core.agent_v2.middleware.reloading_skills_middleware import ReloadingSkillsMiddleware
from internal.core.agent_v2.middleware.runtime_context_middleware import (
    RuntimeContextMiddleware,
)
from internal.core.agent_v2.middleware.tool_call_validation_middleware import (
    ToolCallValidationMiddleware,
)
from internal.core.agent_v2.middleware.user_input_middleware import UserInputMiddleware


_RETRYABLE_MODEL_STATUS_CODES = frozenset({408, 409, 429})
_RETRYABLE_READ_ONLY_TOOLS = ("ls", "read_file", "glob", "grep")
_MODEL_CALL_LIMIT = 10
_TOOL_CALL_LIMIT = 40


def build_deep_agent(
    *,
    llm: BaseChatModel,
    app_name: str,
    app_description: str,
    preset_prompt: str,
    review_config: Mapping[str, Any],
    tools: Sequence[BaseTool],
    subagents: Sequence[SubAgentDefinition],
    runtime: AgentRuntimeResources,
) -> CompiledStateGraph:
    """把已校验的应用配置和已解析依赖编译成 Deep Agents 图。"""
    # 1.在 Core 内统一生成提示词和已授权工具，避免调用方了解图装配细节。
    agent_tools = validate_agent_tools(tools)
    system_prompt = build_app_system_prompt(
        app_name=app_name,
        app_description=app_description,
        preset_prompt=preset_prompt,
        memory_enabled=runtime.runtime_context.memory_enabled,
    )
    middleware = _build_platform_middleware(
        review_config=review_config,
        runtime=runtime,
    )

    # 2.将已授权的同步、异步 SubAgent 配置转换为原生规格。
    # 持久化记录仅作业务适配，其余运行时能力由 DeepAgents 负责。
    subagent_specs = build_subagent_specs(
        subagents,
        skill_sources_by_agent=runtime.subagent_skill_sources,
    )

    if runtime.runtime_context.user_input_enabled and runtime.checkpointer:
        middleware.append(UserInputMiddleware())
        system_prompt += (
            "\n当缺少影响任务结果的必要信息时，调用 ask_user 向用户展示表单。"
            "一次收集相关问题，已有答案不要重复询问。等待用户填写后再继续依赖这些信息的工作。"
            "需要用户补充的信息由主 Agent 汇总提问。"
            "工具返回的答案是本任务已确认约束，继续执行时遵守；用户没有提出修改时不要重复提问。"
            "用户标记 unavailable 或 skipped 时，不要声称已获得信息；可行时说明假设继续，否则解释缺少什么。"
            "delegate 只允许代选对应字段，不代表用户授权执行其他操作。优先一次询问最关键的 1 至 3 个问题。"
        )

    # 3.把模型、工具、权限、持久化和上下文边界编译成运行图。
    return create_deep_agent(
        model=llm,
        tools=agent_tools,
        system_prompt=system_prompt,
        middleware=middleware,
        subagents=subagent_specs,
        skills=list(runtime.skill_sources) or None,
        # 记忆由刷新中间件装配，不能同时启用官方默认中间件，
        # 否则会在同一请求链中重复注入记忆提示词。
        memory=None,
        permissions=list(runtime.permissions),
        backend=runtime.backend,
        checkpointer=runtime.checkpointer,
        store=runtime.store,
        context_schema=AgentRunContext,
    )


def _build_platform_middleware(
    *,
    review_config: Mapping[str, Any],
    runtime: AgentRuntimeResources,
) -> list[AgentMiddleware]:
    """按稳定顺序构造 MyDify 扩展的 DeepAgents 中间件。"""
    middleware: list[AgentMiddleware] = [
        ReloadingSkillsMiddleware(
            backend=runtime.backend,
            sources=list(runtime.skill_sources),
            system_prompt=None,
        ),
        ToolCallValidationMiddleware(),
        RuntimeContextMiddleware(),
        ReviewMiddleware(review_config=dict(review_config)),
        ModelRetryMiddleware(
            max_retries=2,
            retry_on=_is_retryable_model_error,
            on_failure="error",
        ),
        # 只有无副作用的内置读取工具可以自动重试；写入和未知业务工具
        # 无法仅凭异常确认前一次调用是否已经生效。
        ToolRetryMiddleware(
            max_retries=2,
            tools=list(_RETRYABLE_READ_ONLY_TOOLS),
            retry_on=(TimeoutError, ConnectionError),
            on_failure="error",
        ),
        ModelCallLimitMiddleware(
            run_limit=_MODEL_CALL_LIMIT,
            exit_behavior="end",
        ),
        ToolCallLimitMiddleware(
            run_limit=_TOOL_CALL_LIMIT,
            exit_behavior="continue",
        ),
    ]
    if runtime.memory_sources:
        middleware.append(ReloadingMemoryMiddleware(backend=runtime.backend, sources=list(runtime.memory_sources)))
    middleware.append(ContextWindowMiddleware())
    return middleware


def _is_retryable_model_error(exc: Exception) -> bool:
    """仅重试当前 OpenAI 兼容模型链路中明确的瞬时故障。"""
    if isinstance(exc, (TimeoutError, ConnectionError, APIConnectionError)):
        return True

    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code in _RETRYABLE_MODEL_STATUS_CODES or 500 <= status_code <= 599
    return False


__all__ = [
    "build_deep_agent",
]
