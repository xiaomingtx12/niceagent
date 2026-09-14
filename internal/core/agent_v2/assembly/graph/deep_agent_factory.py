"""MyDify 的 DeepAgents 图装配入口。"""

from __future__ import annotations

from deepagents import create_deep_agent
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT
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
from internal.core.agent_v2.entity.agent_config import AgentConfig
from internal.core.agent_v2.assembly.graph.platform_middleware import build_platform_middleware
from internal.core.agent_v2.middleware.shell_execution_middleware import ShellExecutionMiddleware


def build_deep_agent(
    *,
    definition: AgentConfig,
    runtime: AgentRuntimeResources,
) -> CompiledStateGraph:
    """把已校验的应用配置和已解析依赖编译成 Deep Agents 图。"""
    # 1.在 Core 内生成提示词和已授权工具，隔离调用方与图装配细节。
    agent_tools = validate_agent_tools(definition.tools)
    # 显式交付与普通文件操作分离，工具成功回执意味着文件已经保存到持久化来源。
    agent_tools.extend(runtime.file_delivery.tools())
    system_prompt = build_app_system_prompt(
        app_name=definition.name,
        app_description=definition.description,
        preset_prompt=definition.preset_prompt,
        memory_enabled=runtime.runtime_context.memory_enabled,
        user_input_enabled=bool(runtime.runtime_context.user_input_enabled and runtime.checkpointer),
    )
    middleware = build_platform_middleware(runtime=runtime)

    # 2.将已授权的同步、异步 SubAgent 配置转换为原生规格。
    # 持久化记录仅作业务适配，其余运行时能力由 DeepAgents 负责。
    subagent_specs = build_subagent_specs(
        definition.subagents,
        skill_sources_by_agent=runtime.skill_projection.subagent_sources,
    )

    # 框架默认通用子 Agent 不继承主 Agent 中间件，却会继承父工具（含 execute）；
    # 这里统一显式声明，按平台策略在执行入口关闭子 Agent 的共享 Shell。
    if not any(spec["name"] == GENERAL_PURPOSE_SUBAGENT["name"] for spec in subagent_specs):
        subagent_specs.insert(0, {
            **GENERAL_PURPOSE_SUBAGENT,
            "skills": list(runtime.skill_projection.sources),
            "middleware": [ShellExecutionMiddleware(
                allow_execution=False,
                virtual_paths=runtime.execution_uses_virtual_paths,
            )],
        })

    # 3.把模型、工具、权限、持久化和上下文边界编译成运行图。
    return create_deep_agent(
        model=definition.model,
        tools=agent_tools,
        system_prompt=system_prompt,
        middleware=middleware,
        subagents=subagent_specs,
        skills=list(runtime.skill_projection.sources) or None,
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
