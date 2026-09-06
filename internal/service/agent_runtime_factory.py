"""创建并管理 Agent 单次运行所需的资源。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from injector import inject
from sqlalchemy.orm import Session, sessionmaker

from config.config import Config
from internal.core.agent_v2.assembly.context.memory_context import MemoryContextAssembler
from internal.core.agent_v2.assembly.context.runtime_resources import AgentRuntimeResources
from internal.core.agent_v2.assembly.context.sandbox_context import (
    SandboxContextAssembler,
    SandboxProjection,
    build_filesystem_permissions,
)
from internal.core.agent_v2.assembly.context.skill_context import SkillContextAssembler
from internal.core.agent_v2.assembly.context.workspace_context import (
    WorkspaceContextAssembler,
    WorkspaceProjection,
)
from internal.core.agent_v2.assembly.observability.run_observer import (
    PlatformAgentRunObserver,
)
from internal.core.agent_v2.assembly.observability.telemetry import AgentTelemetry
from internal.core.agent_v2.backends.composition.composite_backend_factory import (
    build_composite_backend,
    close_runtime_backends,
)
from internal.core.agent_v2.backends.sandbox.sandbox_backend_factory import (
    build_stable_sandbox_name,
)
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.entity.subagent_config import SubAgentConfig
from internal.core.agent_v2.persistence.langgraph_checkpoint_provider import (
    get_configured_checkpointer,
)
from internal.core.agent_v2.persistence.langgraph_store_provider import get_configured_store
from internal.entity import BuiltinSkill
from internal.extension.logging_extension import logger
from internal.repository import WorkspaceFileStore
from internal.schema.agent_runtime_schema import AgentInvocation
from internal.schema.app_config_schema import AgentAppConfigReq
from pkg.sqlalchemy import SQLAlchemy

from .skill_service import SkillService


@inject
@dataclass
class AgentRuntimeFactory:
    """持有运行时资源创建顺序、独立 Session 和失败回滚逻辑。"""

    db: SQLAlchemy
    skill_service: SkillService
    workspace_file_store: WorkspaceFileStore
    config: Config

    # -------------------- Core runtime assembly --------------------

    def create(
        self,
        *,
        invocation: AgentInvocation,
        app_config: AgentAppConfigReq,
        selected_skills: Sequence[str],
        subagents: Sequence[SubAgentConfig],
        observer: PlatformAgentRunObserver,
    ) -> AgentRuntimeResources:
        """创建本次 Run 独占的运行资源，失败时回滚已创建 Backend。"""
        # 1.建立当前 Run 的可信身份和记忆开关。
        run_context = AgentRunContext(
            account_id=invocation.account_id,
            app_id=invocation.app_id,
            conversation_id=invocation.conversation_id,
            created_by=invocation.created_by,
            principal_id=(invocation.end_user_id or invocation.created_by),
            memory_enabled=app_config.long_term_memory.enable,
        )

        # 2.解析当前配置中已启用的 Skill。
        skills = self.skill_service.resolve_enabled_skills(app_config.skills)

        # 3.为 Workspace Backend 创建独立数据库 Session。
        runtime_session = sessionmaker(
            bind=self.db.engine,
            expire_on_commit=False,
        )()
        try:
            # 4.按依赖顺序装配所有运行时 Backend 和持久化资源。
            return self._assemble(
                runtime_session=runtime_session,
                run_context=run_context,
                skills=skills,
                selected_skills=selected_skills,
                subagents=subagents,
                observer=observer,
            )
        except Exception:
            # 5.装配失败时关闭独立 Session，并保留原始异常。
            runtime_session.close()
            raise

    def _assemble(
        self,
        *,
        runtime_session: Session,
        run_context: AgentRunContext,
        skills: list[BuiltinSkill],
        selected_skills: Sequence[str],
        subagents: Sequence[SubAgentConfig],
        observer: PlatformAgentRunObserver,
    ) -> AgentRuntimeResources:
        """按依赖顺序创建 Workspace、Skill、Memory、Sandbox 和路由。"""
        # 1.创建统一的运行时观测器和资源清理跟踪变量。
        telemetry = AgentTelemetry(
            observer,
            attributes=run_context.telemetry_attributes,
        )
        workspace_projection: WorkspaceProjection | None = None
        sandbox_projection: SandboxProjection | None = None
        try:
            # 2.创建连接业务 Workspace 的 Backend。
            workspace_projection = WorkspaceContextAssembler().build(
                runtime_session=runtime_session,
                run_context=run_context,
                settings=self.config,
                telemetry=telemetry,
                file_store=self.workspace_file_store,
            )

            # 3.把主 Agent 和 SubAgent 的 Skill 投影为只读文件路由。
            skill_projection = SkillContextAssembler().build(
                skills=skills,
                selected_skill_names=selected_skills,
                subagents=subagents,
            )

            # 4.按配置创建长期记忆 Store 和 /memories/ 路由。
            memory_projection = MemoryContextAssembler(
                store_provider=get_configured_store,
            ).build(enabled=run_context.memory_enabled)

            # 5.创建可选 Sandbox，并同步 Workspace、Skill 初始文件。
            sandbox_projection = SandboxContextAssembler().build(
                settings=self.config,
                workspace=workspace_projection.backend,
                sandbox_name=build_stable_sandbox_name(
                    account_id=run_context.account_id,
                    app_id=run_context.app_id,
                    conversation_id=run_context.conversation_id,
                    agent_id=run_context.agent_id,
                ),
                skill_projection=skill_projection,
                telemetry=telemetry,
            )

            # 6.合并所有路径路由，并获取 LangGraph Checkpointer。
            backend = build_composite_backend(
                routes={
                    **workspace_projection.routes,
                    **skill_projection.routes,
                    **memory_projection.routes,
                },
                sandbox=(
                    sandbox_projection.backend
                    if sandbox_projection is not None
                    else None
                ),
            )
            checkpointer = get_configured_checkpointer()
        except Exception:
            # 7.Workspace 和 Sandbox 可能已创建；清理异常不得覆盖装配异常。
            for backend_to_close in (
                sandbox_projection.backend if sandbox_projection is not None else None,
                workspace_projection.backend if workspace_projection is not None else None,
            ):
                if backend_to_close is None:
                    continue
                try:
                    close_runtime_backends(backend_to_close)
                except Exception:
                    logger.exception("关闭运行时 Backend 失败")
            raise

        # 8.返回交给 DeepAgents 图和 AgentExecutor 使用的资源快照。
        return AgentRuntimeResources(
            checkpointer=checkpointer,
            backend=backend,
            permissions=build_filesystem_permissions(
                memory_enabled=run_context.memory_enabled,
            ),
            skill_sources=skill_projection.sources,
            memory_sources=memory_projection.sources,
            subagent_skill_sources=skill_projection.subagent_sources,
            telemetry=telemetry,
            store=memory_projection.store,
            runtime_context=run_context,
        )

    # -------------------- Runtime cleanup helper --------------------

    @staticmethod
    def close(runtime: AgentRuntimeResources | None) -> None:
        """关闭尚未交给 AgentExecutor 的资源，不覆盖原始异常。"""
        # 1.没有成功创建运行时资源时无需清理。
        if runtime is None:
            return

        # 2.关闭 CompositeBackend 及其持有的 Workspace/Sandbox 资源。
        try:
            close_runtime_backends(runtime.backend)
        except Exception:
            logger.exception("关闭未接管的 Agent Backend 失败")


__all__ = ["AgentRuntimeFactory"]
