"""Workspace 文件元数据仓储。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from threading import RLock
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from internal.core.agent_v2.entity.workspace_quota import (
    AgentWorkspaceLimits,
)
from internal.model import WorkspaceFile


class WorkspaceFileRevisionConflict(RuntimeError):
    """并发写入同一逻辑路径冲突时抛出。"""


class WorkspaceStorageError(RuntimeError):
    """Workspace 内容存储不可用或返回无效数据。"""


class WorkspaceFileRepository:
    """维护单个账号、应用与 Workspace 作用域内的文件元数据。"""

    def __init__(
        self,
        session: Session,
        account_id: UUID,
        app_id: UUID,
        scope_id: UUID,
        limits: AgentWorkspaceLimits,
        created_by: UUID | None = None,
    ) -> None:
        """绑定数据库会话、访问作用域和配额规则。"""
        self._session = session
        self.account_id = account_id
        self.app_id = app_id
        self.scope_id = scope_id
        self.created_by = created_by
        self._limits = limits
        self._session_lock = RLock()
        lock_scope = f"workspace:{account_id}:{app_id}:{scope_id}"
        self._scope_lock_key = int.from_bytes(
            sha256(lock_scope.encode("ascii")).digest()[:8], "big", signed=True
        )

    def close(self) -> None:
        """关闭运行时持有的独立数据库会话。"""
        with self._session_lock:
            self._session.close()

    def fetch(
        self,
        path: str,
        *,
        include_deleted: bool = False,
    ) -> WorkspaceFile | None:
        """读取当前作用域内指定逻辑路径的元数据。"""
        stmt = select(WorkspaceFile).where(
            *self._scope_filters(), WorkspaceFile.path == path
        )
        if not include_deleted:
            stmt = stmt.where(WorkspaceFile.is_deleted.is_(False))
        with self._session_lock:
            return self._session.execute(
                stmt.execution_options(populate_existing=True)
            ).scalar_one_or_none()

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
        stmt = stmt.order_by(WorkspaceFile.path.asc()).execution_options(
            populate_existing=True
        )
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
        expected_revision: int | None = None,
    ) -> tuple[WorkspaceFile, str | None]:
        """提交元数据；版本 0 表示目标应不存在，None 表示无版本前置条件。"""
        if size_bytes < 0:
            raise ValueError("Workspace 文件大小不能小于 0")
        with self._write_transaction():
            row = self._locked_file(path)
            self._check_revision(row, expected_revision)
            if enforce_quota:
                self._check_quota(row, new_size=size_bytes)

            now = datetime.now(timezone.utc)
            old_object_key = row.object_key if row is not None else None
            if row is None:
                row = WorkspaceFile(
                    id=uuid4(),
                    account_id=self.account_id,
                    app_id=self.app_id,
                    # conversation_id 是既有物理字段，当前保存稳定 Workspace 作用域。
                    conversation_id=self.scope_id,
                    path=path,
                    revision=1,
                    created_by=self.created_by,
                    created_at=now,
                    updated_at=now,
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
        return row, old_object_key

    def mark_deleted(
        self, path: str, *, expected_revision: int | None = None
    ) -> str | None:
        """按读取版本删除，避免旧 Run 删除其他 Run 刚更新的文件。"""
        with self._write_transaction():
            row = self._locked_file(path)
            self._check_revision(row, expected_revision)
            if row is None or row.is_deleted:
                return None
            object_key = row.object_key
            row.is_deleted = True
            row.revision += 1
            row.updated_at = datetime.now(timezone.utc)
        return object_key

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        """串行化 PostgreSQL 同一 Workspace 的用量变更，提交或回滚时释放锁。"""
        with self._session_lock:
            try:
                if self._session.get_bind().dialect.name == "postgresql":
                    self._session.execute(
                        select(func.pg_advisory_xact_lock(self._scope_lock_key))
                    )
                yield
                self._session.commit()
            except IntegrityError as exc:
                self._session.rollback()
                constraint = getattr(
                    getattr(exc.orig, "diag", None), "constraint_name", None
                )
                if constraint == "uq_workspace_files_scope_path":
                    raise WorkspaceFileRevisionConflict(
                        "Workspace 文件正在被并发创建，请重试"
                    ) from exc
                raise
            except Exception:
                self._session.rollback()
                raise

    def _locked_file(self, path: str) -> WorkspaceFile | None:
        """锁后刷新 ORM 缓存，防止长生命周期 Session 使用旧版本。"""
        stmt = (
            select(WorkspaceFile)
            .where(*self._scope_filters(), WorkspaceFile.path == path)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    @staticmethod
    def _check_revision(
        row: WorkspaceFile | None, expected_revision: int | None
    ) -> None:
        """删除态视为逻辑不存在；历史版本不能覆盖重建后的文件。"""
        if expected_revision is None:
            return
        if expected_revision < 0:
            raise ValueError("Workspace 文件版本不能小于 0")
        actual_revision = row.revision if row is not None and not row.is_deleted else 0
        if actual_revision != expected_revision:
            raise WorkspaceFileRevisionConflict(
                f"Workspace 文件版本冲突：预期 {expected_revision}，"
                f"当前 {actual_revision}，请重新读取后重试"
            )

    def _check_quota(self, current: WorkspaceFile | None, *, new_size: int) -> None:
        """在作用域锁内聚合用量，覆盖或恢复文件时只计算净增量。"""
        files, size_bytes, minimum_size = self._session.execute(
            select(
                func.count(WorkspaceFile.id),
                func.coalesce(func.sum(WorkspaceFile.size_bytes), 0),
                func.coalesce(func.min(WorkspaceFile.size_bytes), 0),
            ).where(*self._scope_filters(), WorkspaceFile.is_deleted.is_(False))
        ).one()
        if minimum_size < 0:
            raise ValueError("Workspace 已有文件大小不能小于 0")
        replacing = current is not None and not current.is_deleted
        self._limits.check_usage(
            files=files + (0 if replacing else 1),
            size_bytes=size_bytes - (current.size_bytes if replacing else 0) + new_size,
        )

    def _scope_filters(self) -> tuple:
        """返回账号、应用和 Workspace 作用域隔离条件。"""
        return (
            WorkspaceFile.account_id == self.account_id,
            WorkspaceFile.app_id == self.app_id,
            WorkspaceFile.conversation_id == self.scope_id,
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
