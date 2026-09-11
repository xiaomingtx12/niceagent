"""已授权能力到 Agent 执行器的装配与资源交接。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from deepagents.backends.protocol import BackendProtocol
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from config.config import Config
from internal.core.agent_v2.assembly.context.memory_context import build_memory_context
from internal.core.agent_v2.execution.runtime_resources import AgentRuntimeResources
from internal.core.agent_v2.assembly.context.sandbox_context import (
    build_filesystem_permissions,
)
from internal.core.agent_v2.assembly.context.skill_context import build_skill_context
from internal.core.agent_v2.backends.workspace.workspace_backend import WorkspaceBackend
from internal.core.agent_v2.assembly.graph.deep_agent_factory import build_deep_agent
from internal.core.agent_v2.observability.telemetry import AgentTelemetry
from internal.core.agent_v2.observability.run_observer import (
    PlatformAgentRunObserver,
)
from internal.core.agent_v2.backends.composition.composite_backend_factory import (
    build_composite_backend,
    close_runtime_backends,
)
from internal.core.agent_v2.backends.sandbox.sandbox_backend_factory import (
    build_stable_sandbox_name,
    build_sandbox_backend,
)
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.entity.agent_config import AgentConfig
from internal.core.agent_v2.execution.agent_executor import AgentExecutor
from internal.core.agent_v2.persistence.langgraph_checkpoint_provider import (
    get_configured_checkpointer,
)
from internal.core.agent_v2.persistence.langgraph_store_provider import get_configured_store
from internal.extension.logging_extension import logger


@dataclass
class AgentRuntimeAssembler:
    """接收已解析能力，创建运行资源；成功交给执行器，失败则关闭全部资源。"""

    workspace_factory: Callable[[AgentRunContext, AgentTelemetry], WorkspaceBackend]
    config: Config

    store_provider: Callable[[], BaseStore] = get_configured_store
    checkpointer_provider: Callable[[], BaseCheckpointSaver | bool] = get_configured_checkpointer

    def build(
        self,
        *,
        context: AgentRunContext,
        definition: AgentConfig,
        observer: PlatformAgentRunObserver,
        prepare_backend: Callable[[BackendProtocol], None],
    ) -> AgentExecutor:
        """先取得运行资源，再准备附件和编译图；失败时保留原始装配错误。"""
        started_at = time.monotonic()
        runtime = self._build_runtime(context, definition, observer)
        try:
            self._record_stage(observer, context, "runtime", started_at)
            started_at = time.monotonic()
            prepare_backend(runtime.backend)
            self._record_stage(observer, context, "attachments", started_at)
            started_at = time.monotonic()
            agent = self._build_executor(runtime, definition, observer)
            self._record_stage(observer, context, "graph", started_at)
            return agent
        except BaseException:
            try:
                runtime.close()
            except Exception:
                logger.exception("Agent 装配失败后的 Backend 清理失败")
            raise

    def _build_runtime(
        self,
        context: AgentRunContext,
        definition: AgentConfig,
        observer: PlatformAgentRunObserver,
    ) -> AgentRuntimeResources:
        """逐项登记独占资源，组合成功后将所有权交给本轮运行时。"""
        owned_backends: list[BackendProtocol] = []
        try:
            telemetry = AgentTelemetry(observer.record, attributes=context.telemetry_attributes)
            workspace = self.workspace_factory(context, telemetry)
            owned_backends.append(workspace)
            skills = build_skill_context(
                skills=definition.skills,
                selected_skill_names=definition.selected_skills,
                subagents=definition.subagents,
            )
            owned_backends.extend(skills.routes.values())
            memory = build_memory_context(
                store_provider=self.store_provider,
                enabled=context.memory_enabled,
            )
            owned_backends.extend(memory.routes.values())
            sandbox = build_sandbox_backend(
                settings=self.config,
                workspace=workspace,
                sandbox_name=build_stable_sandbox_name(
                    account_id=context.account_id,
                    app_id=context.app_id,
                    conversation_id=context.conversation_id,
                    agent_id=context.agent_id,
                ),
                skill_files=skills.files,
                telemetry=telemetry,
            )
            if sandbox is not None:
                # 沙箱接管 Workspace，包括尚未启动的惰性沙箱。
                owned_backends[0] = sandbox
            backend = build_composite_backend(
                routes={"/workspace/": workspace, **skills.routes, **memory.routes},
                sandbox=sandbox,
            )
            # 组合成功后只关闭根 Backend，避免与单项清理重复同步。
            owned_backends = [backend]
            return AgentRuntimeResources(
                checkpointer=self.checkpointer_provider(),
                backend=backend,
                permissions=build_filesystem_permissions(memory_enabled=context.memory_enabled),
                skill_sources=skills.sources,
                memory_sources=memory.sources,
                subagent_skill_sources=skills.subagent_sources,
                store=memory.store,
                runtime_context=context,
            )
        except BaseException:
            try:
                close_runtime_backends(*reversed(owned_backends))
            except Exception:
                logger.exception("Agent 运行资源装配失败后的清理失败")
            raise

    @staticmethod
    def _build_executor(
        runtime: AgentRuntimeResources,
        definition: AgentConfig,
        observer: PlatformAgentRunObserver,
    ) -> AgentExecutor:
        """编译阶段只消费已取得的资源，成功后由执行器接管其生命周期。"""
        graph = build_deep_agent(
            llm=definition.model,
            app_name=definition.name,
            app_description=definition.description,
            preset_prompt=definition.preset_prompt,
            review_config=definition.review_config,
            tools=definition.tools,
            subagents=definition.subagents,
            runtime=runtime,
        )
        return AgentExecutor(
            graph=graph, runtime=runtime, observer=observer,
            review_config=definition.review_config,
        )

    @staticmethod
    def _record_stage(
        observer: PlatformAgentRunObserver,
        context: AgentRunContext,
        stage: str,
        started_at: float,
    ) -> None:
        """在阶段完成时记录耗时，后续装配失败也保留已完成阶段。"""
        duration_ms = (time.monotonic() - started_at) * 1000
        observer.record_stage(f"assembly.{stage}", duration_ms)
        logger.info(
            "agent_assembly_timing app_id=%s conversation_id=%s stage=%s duration_ms=%.2f",
            context.app_id, context.conversation_id, stage, duration_ms,
        )


__all__ = ["AgentRuntimeAssembler"]
