"""Agent Workspace 容量限制与配额规则。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceQuotaExceeded(ValueError):
    """写入超过 Workspace 文件数量或总容量限制。"""

    def __init__(self, limit: str, maximum: int) -> None:
        """保存超限维度和上限，供业务边界生成稳定提示。"""
        self.limit = limit
        self.maximum = maximum
        super().__init__(f"Workspace {limit} quota exceeded ({maximum})")


class WorkspaceFileUsage(Protocol):
    """配额计算需要的最小文件视图。"""

    path: str
    size_bytes: int
    is_deleted: bool


class AgentWorkspaceLimits(BaseModel):
    """单个账号应用 Workspace 的容量上限。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_bytes: int = Field(gt=0, strict=True)
    max_files: int = Field(gt=0, strict=True)


DEFAULT_AGENT_WORKSPACE_LIMITS = AgentWorkspaceLimits(
    max_bytes=100 * 1024 * 1024,
    max_files=1000,
)


class AgentWorkspaceQuotaPolicy:
    """计算文件版本写入后的预计 Workspace 用量。"""

    def __init__(self, limits: AgentWorkspaceLimits) -> None:
        """绑定不可变的容量限制。"""
        self._limits = limits

    def check(
        self,
        rows: Iterable[WorkspaceFileUsage],
        *,
        path: str,
        new_size: int,
    ) -> None:
        """拒绝文件数量或字节数超过上限的写入。"""
        live = [row for row in rows if not row.is_deleted]
        current = next((row for row in live if row.path == path), None)
        projected_files = len(live) + (0 if current is not None else 1)
        projected_bytes = (
            sum(int(row.size_bytes or 0) for row in live)
            - (int(current.size_bytes or 0) if current is not None else 0)
            + new_size
        )
        if projected_files > self._limits.max_files:
            raise WorkspaceQuotaExceeded("file", self._limits.max_files)
        if projected_bytes > self._limits.max_bytes:
            raise WorkspaceQuotaExceeded("byte", self._limits.max_bytes)


__all__ = [
    "AgentWorkspaceLimits",
    "AgentWorkspaceQuotaPolicy",
    "DEFAULT_AGENT_WORKSPACE_LIMITS",
    "WorkspaceQuotaExceeded",
    "WorkspaceFileUsage",
]
