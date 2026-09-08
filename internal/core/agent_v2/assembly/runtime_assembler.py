"""已授权能力到 Agent 执行器的装配与资源交接。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from deepagents.backends.protocol import BackendProtocol

from config.config import Config
from internal.core.agent_v2.assembly.context.memory_context import MemoryContextAssembler
from internal.core.agent_v2.entity.runtime_resources import AgentRuntimeResources
from internal.core.agent_v2.assembly.context.sandbox_context import (
    SandboxContextAssembler,
    SandboxProjection,
    build_filesystem_permissions,
)
from internal.core.agent_v2.assembly.context.skill_context import SkillContextAssembler
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

    def build(
        self,
        *,
        context: AgentRunContext,
        definition: AgentConfig,
        observer: PlatformAgentRunObserver,
        prepare_backend: Callable[[BackendProtocol], None],
    ) -> AgentExecutor:
        """装配运行时和图；平台通过回调写入附件，不接管运行资源的生命周期。"""
        # 1.接收平台创建的 Workspace，其底层 Session 由实现自己管理。
        workspace: WorkspaceBackend | None = None
        sandbox: SandboxProjection | None = None
        backend: BackendProtocol | None = None
        skill_projection = None
        phase_started_at = time.monotonic()
        try:
            telemetry = AgentTelemetry(observer, attributes=context.telemetry_attributes)
            workspace = self.workspace_factory(context, telemetry)
            skill_projection = SkillContextAssembler().build(
                skills=list(definition.skills),
                selected_skill_names=definition.selected_skills,
                subagents=definition.subagents,
            )
            memory = MemoryContextAssembler(store_provider=get_configured_store).build(
                enabled=context.memory_enabled,
            )
            sandbox = SandboxContextAssembler().build(
                settings=self.config,
                workspace=workspace,
                sandbox_name=build_stable_sandbox_name(
                    account_id=context.account_id,
                    app_id=context.app_id,
                    conversation_id=context.conversation_id,
                    agent_id=context.agent_id,
                ),
                skill_projection=skill_projection,
                telemetry=telemetry,
            )

            # 2.组合 Backend 和持久化资源，后续失败由组合 Backend 统一清理。
            backend = build_composite_backend(
                routes={
                    "/workspace/": workspace,
                    **skill_projection.routes,
                    **memory.routes,
                },
                sandbox=sandbox.backend if sandbox is not None else None,
            )
            runtime = AgentRuntimeResources(
                checkpointer=get_configured_checkpointer(),
                backend=backend,
                permissions=build_filesystem_permissions(
                    memory_enabled=context.memory_enabled,
                ),
                skill_sources=skill_projection.sources,
                memory_sources=memory.sources,
                subagent_skill_sources=skill_projection.subagent_sources,
                telemetry=telemetry,
                store=memory.store,
                runtime_context=context,
            )
            runtime_ms = (time.monotonic() - phase_started_at) * 1000
            observer.record_stage("assembly.runtime", runtime_ms)

            # 3.平台写入已授权附件后，Core 编译图并创建一次性执行器。
            phase_started_at = time.monotonic()
            prepare_backend(backend)
            attachments_ms = (time.monotonic() - phase_started_at) * 1000
            observer.record_stage("assembly.attachments", attachments_ms)

            phase_started_at = time.monotonic()
            graph = build_deep_agent(
                llm=definition.model,
                app_name=definition.name,
                app_description=definition.description,
                preset_prompt=definition.preset_prompt,
                review_config=definition.review_config,
                tools=definition.tools,
                subagents=definition.subagents,
                runtime=runtime,
                subagent_bindings=definition.subagent_bindings,
            )
            agent = AgentExecutor(
                graph=graph, runtime=runtime, observer=observer,
                review_config=definition.review_config,
            )
            graph_ms = (time.monotonic() - phase_started_at) * 1000
            observer.record_stage("assembly.graph", graph_ms)
            for stage, duration_ms in (
                ("runtime", runtime_ms),
                ("attachments", attachments_ms),
                ("graph", graph_ms),
            ):
                logger.info(
                    "agent_assembly_timing app_id=%s conversation_id=%s "
                    "stage=%s duration_ms=%.2f",
                    context.app_id,
                    context.conversation_id,
                    stage,
                    duration_ms,
                )
            return agent
        except Exception:
            # 4.只关闭当前资源所有者，避免 Sandbox 与 Workspace 重复释放。
            if backend is not None:
                resources = [backend]
            elif sandbox is not None:
                # Sandbox 成功创建后已接管 Workspace，关闭时会同步并关闭它。
                resources = [sandbox.backend]
            elif workspace is not None:
                resources = [workspace]
            else:
                resources = []
            if backend is None and skill_projection is not None:
                resources.extend(skill_projection.routes.values())
            for resource in resources:
                try:
                    close_runtime_backends(resource)
                except Exception:
                    logger.exception("Agent 装配失败后的 Backend 清理失败")
            raise


__all__ = ["AgentRuntimeAssembler"]
