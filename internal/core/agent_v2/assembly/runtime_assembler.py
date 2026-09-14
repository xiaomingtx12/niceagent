"""已授权能力到 Agent 执行器的装配与资源交接。"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.protocol import BackendProtocol
from langgraph.store.base import BaseStore

from config.config import Config
from internal.core.agent_v2.backends.memory.agent_memory_backend import AgentMemoryBackend, memory_namespace_from_runtime
from internal.core.agent_v2.entity.agent_memory import AGENT_MEMORY_INDEX
from internal.core.agent_v2.execution.runtime_resources import AgentRuntimeResources, close_runtime_backends
from internal.core.agent_v2.assembly.context.sandbox_context import (
    build_filesystem_permissions,
)
from internal.core.agent_v2.assembly.context.skill_context import SkillProjection, build_skill_context
from internal.core.agent_v2.backends.workspace.workspace_projection import WorkspaceProjection
from internal.core.agent_v2.persistence.workspace_store import WorkspaceDeliveryError, WorkspaceStore
from internal.core.agent_v2.execution.workspace_delivery import WorkspaceDelivery
from internal.core.agent_v2.entity.workspace_path import AgentWorkspacePathPolicy
from internal.core.agent_v2.assembly.graph.deep_agent_factory import build_deep_agent
from internal.core.agent_v2.observability.run_observer import (
    PlatformAgentRunObserver,
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
    """接收平台已授权的存储对象，直接装配框架资源；成功交给执行器，失败负责清理。"""

    config: Config
    workspace_store: WorkspaceStore

    def build(
        self,
        *,
        context: AgentRunContext,
        definition: AgentConfig,
        observer: PlatformAgentRunObserver,
        input_files: Sequence[tuple[str, bytes]] = (),
    ) -> AgentExecutor:
        """先取得运行资源，再准备附件和编译图；失败时保留原始装配错误。"""
        started_at = time.monotonic()
        runtime = self._build_runtime(context, definition)
        try:
            self._log_stage(context, "runtime", started_at)
            started_at = time.monotonic()
            # 附件已由服务层读取并校验归属，Core 只将字节写入本轮工作副本。
            if input_files:
                responses = runtime.backend.upload_files(list(input_files))
                if len(responses) != len(input_files) or any(response.error for response in responses):
                    raise WorkspaceDeliveryError("附件写入 Agent Workspace 失败")
            self._log_stage(context, "attachments", started_at)
            started_at = time.monotonic()
            graph = build_deep_agent(definition=definition, runtime=runtime)
            agent = AgentExecutor(graph=graph, runtime=runtime, observer=observer)
            self._log_stage(context, "graph", started_at)
            return agent
        except BaseException:
            try:
                runtime.close()
            except BaseException:
                logger.exception("Agent 装配失败后的 Backend 清理失败")
            raise

    def _build_runtime(
        self,
        context: AgentRunContext,
        definition: AgentConfig,
    ) -> AgentRuntimeResources:
        """逐项登记独占资源，组合成功后将所有权交给本轮运行时。"""
        owned_backends: list[BackendProtocol] = []
        skills: SkillProjection | None = None
        workspace: WorkspaceProjection | None = None
        # build 接管本轮交付存储，即使临时目录创建失败也要释放它。
        workspace_store = self.workspace_store
        try:
            # 1.创建空的本轮副本；持久化访问仅绑定依赖，不查询或下载文件。
            workspace = WorkspaceProjection()
            # 2.装配 Skill 与 Memory 路由，并立即纳入失败清理范围。
            skills = build_skill_context(
                skills=definition.skills,
                selected_skill_names=definition.selected_skills,
                subagents=definition.subagents,
            )
            # Memory 没有投影资源，仅在启用时获取共享 Store 并直接建立路由。
            store: BaseStore | None = None
            memory_routes: dict[str, BackendProtocol] = {}
            if context.memory_enabled:
                store = get_configured_store()
                if store is None:
                    raise RuntimeError("长期记忆 Store 未配置，Agent 运行时拒绝在无记忆模式下启动")
                memory_routes["/memories/"] = AgentMemoryBackend(
                    store=store, namespace=memory_namespace_from_runtime,
                )
            owned_backends.extend(memory_routes.values())
            # 3.Sandbox 只借用 Workspace；运行资源分别持有两者的释放责任。
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
            )
            if sandbox is not None:
                owned_backends.append(sandbox)
            # 4.就地选择工作副本的数据源，路由一次构造，不先添加再过滤 Workspace。
            if sandbox is None:
                backend = CompositeBackend(
                    default=StateBackend(),
                    routes={"/workspace/": workspace.backend, **skills.routes, **memory_routes},
                    artifacts_root="/workspace",
                )
            else:
                # DeepAgents 0.6 的执行权限编译要求显式根路由；较长 Skill/Memory 前缀优先。
                backend = CompositeBackend(
                    default=sandbox,
                    routes={**skills.routes, **memory_routes, "/": sandbox},
                    artifacts_root="/workspace",
                )
            # 临时文件和存储会话分别由资源对象释放，Backend 不持有业务 Session。
            owned_backends = [backend]
            # 5.获取持久化能力并冻结本轮权限与上下文，交给执行器接管。
            return AgentRuntimeResources(
                checkpointer=get_configured_checkpointer(),
                backend=backend,
                workspace=workspace,
                workspace_store=workspace_store,
                file_delivery=WorkspaceDelivery(
                    backend=backend, store=workspace_store,
                    paths=AgentWorkspacePathPolicy(scope_id=context.workspace_scope_id),
                ),
                permissions=build_filesystem_permissions(memory_enabled=context.memory_enabled),
                skill_projection=skills,
                memory_sources=[AGENT_MEMORY_INDEX] if context.memory_enabled else [],
                store=store,
                runtime_context=context,
            )
        except BaseException:
            try:
                close_runtime_backends(*reversed(owned_backends))
            except BaseException:
                logger.exception("Agent 运行资源装配失败后的清理失败")
            for resource in (workspace_store, workspace, skills):
                if resource is None:
                    continue
                try:
                    resource.close()
                except BaseException:
                    logger.exception("Agent 装配失败后的运行资源清理失败")
            raise

    @staticmethod
    def _log_stage(
        context: AgentRunContext,
        stage: str,
        started_at: float,
    ) -> None:
        """在阶段完成时记录耗时，后续装配失败也保留已完成阶段。"""
        duration_ms = (time.monotonic() - started_at) * 1000
        logger.info(
            "agent_assembly_timing app_id=%s conversation_id=%s stage=%s duration_ms=%.2f",
            context.app_id, context.conversation_id, stage, duration_ms,
        )


__all__ = ["AgentRuntimeAssembler"]
