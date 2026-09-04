"""Agent Workspace 容量限制与配额规则。"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceQuotaKind(str, Enum):
    """Workspace 支持的配额维度。"""

    FILES = "files"
    BYTES = "bytes"


class WorkspaceQuotaExceeded(ValueError):
    """写入超过 Workspace 文件数量或总容量限制。"""

    def __init__(self, kind: WorkspaceQuotaKind, maximum: int) -> None:
        """保存超限维度和上限，供业务边界生成稳定提示。"""
        self.kind = kind
        self.maximum = maximum
        super().__init__(f"Workspace {kind.value} quota exceeded ({maximum})")


class _WorkspaceFileUsage(Protocol):
    """配额计算需要的最小文件视图。"""

    path: str
    size_bytes: int
    is_deleted: bool


class AgentWorkspaceLimits(BaseModel):
    """单个账号与应用 Workspace 的容量规则。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_bytes: int = Field(gt=0, strict=True)
    max_files: int = Field(gt=0, strict=True)

    def check_write(
        self,
        rows: Iterable[_WorkspaceFileUsage],
        *,
        path: str,
        new_size: int,
    ) -> None:
        """校验新增或覆盖文件后的数量与总字节数。"""
        if new_size < 0:
            raise ValueError("Workspace 文件大小不能小于 0")

        live = [row for row in rows if not row.is_deleted]
        if any(row.size_bytes < 0 for row in live):
            raise ValueError("Workspace 已有文件大小不能小于 0")
        current = next((row for row in live if row.path == path), None)
        projected_files = len(live) + (0 if current is not None else 1)
        projected_bytes = (
            sum(row.size_bytes for row in live)
            - (current.size_bytes if current is not None else 0)
            + new_size
        )
        if projected_files > self.max_files:
            raise WorkspaceQuotaExceeded(WorkspaceQuotaKind.FILES, self.max_files)
        if projected_bytes > self.max_bytes:
            raise WorkspaceQuotaExceeded(WorkspaceQuotaKind.BYTES, self.max_bytes)


__all__ = [
    "AgentWorkspaceLimits",
    "WorkspaceQuotaExceeded",
    "WorkspaceQuotaKind",
]
