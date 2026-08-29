"""Workspace 运行时资源装配。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from sqlalchemy.orm import Session

from internal.core.agent_v2.backends.workspace_backend import AgentWorkspaceBackend
from internal.core.agent_v2.entity.workspace_quota import AgentWorkspaceLimits
from internal.core.agent_v2.backends.workspace_route_backend import (
    AgentWorkspaceRouteBackend,
)
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.assembly.observability.telemetry import AgentTelemetry
from internal.core.agent_v2.persistence.workspace_file_storage_protocol import (
    WorkspaceFileStorageProtocol,
)


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
        storage_service: WorkspaceFileStorageProtocol,
    ) -> WorkspaceProjection:
        """创建本次运行独立的 Workspace Backend。"""
        workspace = AgentWorkspaceBackend(
            session=runtime_session,
            account_id=run_context.account_id,
            app_id=run_context.app_id,
            conversation_id=run_context.conversation_id,
            storage_service=storage_service,
            created_by=run_context.created_by,
            limits=AgentWorkspaceLimits(
                max_bytes=settings.WORKSPACE_MAX_BYTES,
                max_files=settings.WORKSPACE_MAX_FILES,
            ),
            telemetry=telemetry,
        )
        return WorkspaceProjection(
            backend=workspace,
            routes={
                "/workspace/": AgentWorkspaceRouteBackend(
                    workspace,
                    "/workspace/",
                    close_owner=True,
                ),
            },
        )


__all__ = ["WorkspaceContextAssembler", "WorkspaceProjection"]
