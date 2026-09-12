"""MyDify 的 DeepAgents 图装配入口。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from deepagents import create_deep_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

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
from internal.core.agent_v2.assembly.graph.platform_middleware import build_platform_middleware


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
        user_input_enabled=bool(runtime.runtime_context.user_input_enabled and runtime.checkpointer),
    )
    middleware = build_platform_middleware(
        review_config=review_config,
        runtime=runtime,
    )

    # 2.将已授权的同步、异步 SubAgent 配置转换为原生规格。
    # 持久化记录仅作业务适配，其余运行时能力由 DeepAgents 负责。
    subagent_specs = build_subagent_specs(
        subagents,
        skill_sources_by_agent=runtime.subagent_skill_sources,
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


__all__ = [
    "build_deep_agent",
]
