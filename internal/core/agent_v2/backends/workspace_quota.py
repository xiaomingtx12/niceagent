"""独立于持久化和传输的 Workspace 配额。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol


class WorkspaceSizedFile(Protocol):
    """配额计算所需的最小文件协议。"""

    path: str
    size_bytes: int
    is_deleted: bool


class WorkspaceQuotaExceeded(ValueError):
    """写入超过 Workspace 容量时抛出。"""

    def __init__(self, limit: str, maximum: int) -> None:
        """描述超限维度且不包含文件内容。"""
        self.limit = limit
        self.maximum = maximum
        super().__init__(f"Workspace {limit} quota exceeded ({maximum})")


@dataclass(frozen=True)
class WorkspaceLimits:
    """单个账号应用 Workspace 的容量上限。"""

    max_bytes: int
    max_files: int

    def __post_init__(self) -> None:
        """拒绝关闭或无效的安全限制。"""
        if self.max_bytes <= 0 or self.max_files <= 0:
            raise ValueError("Workspace limits must be greater than zero")


DEFAULT_WORKSPACE_LIMITS = WorkspaceLimits(
    max_bytes=100 * 1024 * 1024,
    max_files=1000,
)


class WorkspaceQuotaPolicy:
    """计算版本写入前的预计用量。"""

    def __init__(self, limits: WorkspaceLimits) -> None:
        """绑定不可变配额限制。"""
        self._limits = limits

    def check(
        self,
        rows: Iterable[WorkspaceSizedFile],
        *,
        path: str,
        new_size: int,
    ) -> None:
        """拒绝预计用量超限的写入。"""
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
    "DEFAULT_WORKSPACE_LIMITS",
    "WorkspaceLimits",
    "WorkspaceQuotaExceeded",
    "WorkspaceQuotaPolicy",
]
