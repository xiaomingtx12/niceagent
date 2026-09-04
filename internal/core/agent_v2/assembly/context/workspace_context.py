"""Workspace 运行时资源装配。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from deepagents.backends.protocol import BackendProtocol
from sqlalchemy.orm import Session

from internal.core.agent_v2.backends.workspace.workspace_backend import (
    AgentWorkspaceBackend,
)
from internal.core.agent_v2.entity.workspace_quota import AgentWorkspaceLimits
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.assembly.observability.telemetry import AgentTelemetry

if TYPE_CHECKING:
    from internal.repository import WorkspaceFileStore


@dataclass
class WorkspaceProjection:
    """会话文件和产物的运行时投影。"""

    backend: AgentWorkspaceBackend
    routes: dict[str, BackendProtocol]


class WorkspaceContextAssembler:
    """把业务 Workspace 适配为 DeepAgents 路由。"""

    def build(
        self,
        *,
        runtime_session: Session,
        run_context: AgentRunContext,
        settings: Any,
        telemetry: AgentTelemetry,
        file_store: WorkspaceFileStore,
    ) -> WorkspaceProjection:
        """创建本次运行连接到稳定用户 Workspace 的 Backend。"""
        workspace = AgentWorkspaceBackend(
            session=runtime_session,
            account_id=run_context.account_id,
            app_id=run_context.app_id,
            # Workspace 跨会话共享；conversation_id 仅用于线程状态，不能
            # 继续作为文件存储作用域。
            workspace_scope_id=run_context.workspace_scope_id,
            file_store=file_store,
            created_by=run_context.created_by,
            limits=AgentWorkspaceLimits(
                max_bytes=settings.WORKSPACE_MAX_BYTES,
                max_files=settings.WORKSPACE_MAX_FILES,
            ),
            telemetry=telemetry,
        )
        return WorkspaceProjection(
            backend=workspace,
            routes={"/workspace/": workspace},
        )


__all__ = ["WorkspaceContextAssembler", "WorkspaceProjection"]
