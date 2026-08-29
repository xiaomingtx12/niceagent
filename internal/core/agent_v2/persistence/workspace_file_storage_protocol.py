"""Workspace 内容存储服务的 Core 依赖协议。"""

from __future__ import annotations

from typing import Protocol

from internal.core.agent_v2.persistence.workspace_file_repository import (
    WorkspaceFileRepository,
)
from internal.core.agent_v2.assembly.observability.telemetry import AgentTelemetry
from internal.model import WorkspaceFile


class WorkspaceFileStorageProtocol(Protocol):
    """定义 Backend 所需的最小文件内容能力。"""

    def write_bytes(
        self,
        repository: WorkspaceFileRepository,
        path: str,
        content: bytes,
        *,
        mime_type: str = "application/octet-stream",
        enforce_quota: bool = True,
        telemetry: AgentTelemetry | None = None,
    ) -> WorkspaceFile: ...

    def write_text(
        self,
        repository: WorkspaceFileRepository,
        path: str,
        content: str,
        *,
        mime_type: str = "text/plain",
        enforce_quota: bool = True,
        telemetry: AgentTelemetry | None = None,
    ) -> WorkspaceFile: ...

    def read_bytes(
        self,
        row: WorkspaceFile,
        *,
        max_bytes: int | None = None,
    ) -> bytes: ...

    def delete(
        self,
        repository: WorkspaceFileRepository,
        path: str,
    ) -> None: ...


__all__ = ["WorkspaceFileStorageProtocol"]
