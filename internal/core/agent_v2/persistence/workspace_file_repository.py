"""Agent Workspace 文件元数据仓储。"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from internal.core.agent_v2.entity.workspace_quota import (
    DEFAULT_AGENT_WORKSPACE_LIMITS,
    AgentWorkspaceLimits,
    AgentWorkspaceQuotaPolicy,
)
from internal.model import WorkspaceFile


class WorkspaceFileRevisionConflict(RuntimeError):
    """并发写入同一逻辑路径冲突时抛出。"""


class WorkspaceStorageError(RuntimeError):
    """Workspace 内容存储不可用或返回无效数据。"""


class WorkspaceFileRepository:
    """在一个租户应用作用域内维护当前文件元数据。

    该仓储只服务 Agent Runtime 的独立 Session，封装元数据事务、配额、锁和
    revision；HTTP Workspace Service 的展示查询可以直接使用 SQLAlchemy。
    """

    def __init__(
        self,
        session: Session,
        account_id: UUID,
        app_id: UUID,
        conversation_id: UUID,
        created_by: UUID | None = None,
        limits: AgentWorkspaceLimits = DEFAULT_AGENT_WORKSPACE_LIMITS,
    ) -> None:
        """绑定独立数据库会话、访问作用域和配额。"""
        self._session = session
        self.account_id = account_id
        self.app_id = app_id
        self.conversation_id = conversation_id
        self.created_by = created_by
        self._quota = AgentWorkspaceQuotaPolicy(limits)
        self._session_lock = RLock()

    def close(self) -> None:
        """关闭运行时持有的独立数据库会话。"""
        with self._session_lock:
            self._session.close()

    def fetch(
        self, path: str, *, include_deleted: bool = False
    ) -> WorkspaceFile | None:
        """读取当前逻辑路径的元数据。"""
        stmt = select(WorkspaceFile).where(
            *self._scope_filters(), WorkspaceFile.path == path
        )
        if not include_deleted:
            stmt = stmt.where(WorkspaceFile.is_deleted.is_(False))
        with self._session_lock:
            return self._session.execute(stmt).scalar_one_or_none()

    def list(self, prefixes: tuple[str, ...] | None = None) -> list[WorkspaceFile]:
        """列出当前作用域内未删除文件，可按物理路径前缀过滤。"""
        stmt = select(WorkspaceFile).where(
            *self._scope_filters(),
            WorkspaceFile.is_deleted.is_(False),
        )
        if prefixes:
            path_filters = []
            for prefix in prefixes:
                escaped = self._escape_like(prefix)
                path_filters.extend(
                    [
                        WorkspaceFile.path == prefix,
                        WorkspaceFile.path.like(f"{escaped}/%", escape="\\"),
                    ]
                )
            stmt = stmt.where(or_(*path_filters))
        stmt = stmt.order_by(WorkspaceFile.path.asc())
        with self._session_lock:
            return list(self._session.execute(stmt).scalars().all())

    def save_metadata(
        self,
        *,
        path: str,
        object_key: str,
        mime_type: str,
        size_bytes: int,
        content_sha256: str,
        enforce_quota: bool = True,
    ) -> tuple[WorkspaceFile, str | None]:
        """提交新对象的元数据，并返回被替换的旧对象 key。"""
        with self._session_lock:
            try:
                stmt = (
                    select(WorkspaceFile)
                    .where(*self._scope_filters(), WorkspaceFile.path == path)
                    .with_for_update()
                )
                row = self._session.execute(stmt).scalar_one_or_none()
                if enforce_quota:
                    self._quota.check(self.list(), path=path, new_size=size_bytes)

                now = datetime.now(timezone.utc)
                old_object_key = row.object_key if row is not None else None
                if row is None:
                    row = WorkspaceFile(
                        id=uuid4(),
                        account_id=self.account_id,
                        app_id=self.app_id,
                        conversation_id=self.conversation_id,
                        path=path,
                        revision=1,
                        created_by=self.created_by,
                        created_at=now,
                    )
                    self._session.add(row)
                else:
                    row.revision += 1
                    row.updated_at = now
                    if self.created_by is not None:
                        row.created_by = self.created_by

                row.object_key = object_key
                row.mime_type = mime_type
                row.size_bytes = size_bytes
                row.content_sha256 = content_sha256
                row.is_deleted = False
                self._session.commit()
                return row, old_object_key
            except IntegrityError as exc:
                self._session.rollback()
                raise WorkspaceFileRevisionConflict(
                    "Workspace 文件正在被并发修改，请重试"
                ) from exc
            except Exception:
                self._session.rollback()
                raise

    def mark_deleted(self, path: str) -> str | None:
        """把逻辑路径标记为删除，并返回待清理对象 key。"""
        with self._session_lock:
            try:
                stmt = (
                    select(WorkspaceFile)
                    .where(*self._scope_filters(), WorkspaceFile.path == path)
                    .with_for_update()
                )
                row = self._session.execute(stmt).scalar_one_or_none()
                if row is None or row.is_deleted:
                    return None
                row.is_deleted = True
                row.revision += 1
                row.updated_at = datetime.now(timezone.utc)
                self._session.commit()
                return row.object_key
            except Exception:
                self._session.rollback()
                raise

    def _scope_filters(self) -> tuple:
        """返回租户、应用和会话隔离条件。"""
        return (
            WorkspaceFile.account_id == self.account_id,
            WorkspaceFile.app_id == self.app_id,
            WorkspaceFile.conversation_id == self.conversation_id,
        )

    @staticmethod
    def _escape_like(value: str) -> str:
        """转义 SQL LIKE 元字符。"""
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = [
    "WorkspaceFileRepository",
    "WorkspaceFileRevisionConflict",
    "WorkspaceStorageError",
]
