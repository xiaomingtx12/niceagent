"""Agent V2 Workspace 文件的版本化数据库存储。"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from threading import RLock
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from internal.core.agent_v2.backends.workspace_path import (
    contains_reserved_segment,
    scope_to_conversation_id,
    workspace_path_digest,
)
from internal.core.agent_v2.backends.workspace_quota import (
    DEFAULT_WORKSPACE_LIMITS,
    WorkspaceLimits,
    WorkspaceQuotaExceeded,
    WorkspaceQuotaPolicy,
)
from internal.core.agent_v2.runtime.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.model import WorkspaceFile

_VERSION_RETRIES = 3
_WRITE_LOCKS = tuple(RLock() for _ in range(64))


class WorkspaceVersionConflict(RuntimeError):
    """重试后仍无法分配版本时抛出。"""


class WorkspaceRepository:
    """保存租户应用内的不可变文件版本。"""

    def __init__(
        self,
        session: Session,
        account_id: UUID,
        app_id: UUID,
        conversation_id: Optional[UUID],
        created_by: Optional[UUID] = None,
        limits: WorkspaceLimits = DEFAULT_WORKSPACE_LIMITS,
        telemetry: AgentTelemetry | None = None,
    ) -> None:
        """绑定数据库会话和 Workspace 作用域。"""
        self._session = session
        self._account_id = account_id
        self._app_id = app_id
        self._conversation_id = conversation_id
        self._created_by = created_by
        self._quota = WorkspaceQuotaPolicy(limits)
        self._telemetry = telemetry or NOOP_AGENT_TELEMETRY
        self._session_lock = RLock()
        self._write_lock = _WRITE_LOCKS[
            hash((self._account_id, self._app_id)) % len(_WRITE_LOCKS)
        ]

    def close(self) -> None:
        """关闭独立运行时会话。"""
        with self._session_lock:
            self._session.close()

    def fetch_latest(self, rest: str) -> Optional[WorkspaceFile]:
        """返回路径的最新存储版本。"""
        stmt = (
            select(WorkspaceFile)
            .where(
                WorkspaceFile.account_id == self._account_id,
                WorkspaceFile.app_id == self._app_id,
                WorkspaceFile.path == rest,
            )
            .order_by(WorkspaceFile.version.desc())
            .limit(1)
        )
        with self._session_lock:
            return self._session.execute(stmt).scalar_one_or_none()

    def fetch_current_version(self, rest: str) -> int:
        """返回当前版本号，新路径返回零。"""
        latest = self.fetch_latest(rest)
        return latest.version if latest else 0

    def list_latest(self, prefixes: tuple[str, ...]) -> list[WorkspaceFile]:
        """返回前缀下最新的未删除记录。"""
        path_filters = []
        for item in prefixes:
            escaped = (
                item.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            path_filters.append(WorkspaceFile.path == item)
            path_filters.append(
                WorkspaceFile.path.like(f"{escaped}/%", escape="\\")
            )
        rows = self._latest_versions(or_(*path_filters))
        return [row for row in rows if not row.is_deleted]

    def list_latest_versions(self) -> list[WorkspaceFile]:
        """返回每个路径的最新记录，包含墓碑。"""
        return self._latest_versions()

    def _latest_versions(self, path_filter=None) -> list[WorkspaceFile]:
        """加载并去重应用内的有序路径版本。"""
        stmt = select(WorkspaceFile).where(
            WorkspaceFile.account_id == self._account_id,
            WorkspaceFile.app_id == self._app_id,
        )
        if path_filter is not None:
            stmt = stmt.where(path_filter)
        stmt = stmt.order_by(WorkspaceFile.path, WorkspaceFile.version.desc())
        with self._session_lock:
            rows = self._session.execute(stmt).scalars().all()
        latest_by_path: dict[str, WorkspaceFile] = {}
        for row in rows:
            if row.path not in latest_by_path:
                latest_by_path[row.path] = row
        return list(latest_by_path.values())

    def write_text(
        self,
        rest: str,
        content: str,
        *,
        mime_type: str = "text/plain",
        enforce_quota: bool = True,
    ) -> WorkspaceFile:
        """以 UTF-8 字节写入一个文本快照。"""
        return self.write_bytes(
            rest,
            content.encode("utf-8"),
            mime_type=mime_type,
            enforce_quota=enforce_quota,
        )

    def write_bytes(
        self,
        rest: str,
        content: bytes,
        *,
        mime_type: str = "application/octet-stream",
        enforce_quota: bool = True,
    ) -> WorkspaceFile:
        """经配额检查和冲突重试后追加二进制快照。"""
        encoded = bytes(content)
        with self._write_lock, self._session_lock:
            for attempt in range(_VERSION_RETRIES):
                try:
                    self._acquire_database_write_lock()
                    if enforce_quota:
                        self._quota.check(
                            (
                                row
                                for row in self.list_latest_versions()
                                if not contains_reserved_segment(row.path)
                            ),
                            path=rest,
                            new_size=len(encoded),
                        )
                    now = datetime.now(timezone.utc)
                    row = WorkspaceFile(
                        id=uuid4(),
                        account_id=self._account_id,
                        app_id=self._app_id,
                        conversation_id=scope_to_conversation_id(
                            rest, self._conversation_id
                        ),
                        path=rest,
                        version=self.fetch_current_version(rest) + 1,
                        storage_backend="db",
                        mime_type=mime_type,
                        size_bytes=len(encoded),
                        content_sha256=hashlib.sha256(encoded).hexdigest(),
                        content=encoded,
                        created_by=self._created_by,
                        is_deleted=False,
                        created_at=now,
                        updated_at=now,
                    )
                    self._session.add(row)
                    self._session.commit()
                    self._telemetry.emit(
                        "workspace.write",
                        value=float(len(encoded)),
                        attributes={
                            "path_digest": workspace_path_digest(rest),
                            "scope": rest.split("/", 1)[0],
                            "size_bytes": len(encoded),
                            "version": row.version,
                        },
                    )
                    return row
                except WorkspaceQuotaExceeded as exc:
                    self._session.rollback()
                    self._telemetry.emit(
                        "workspace.quota_rejected",
                        attributes={
                            "limit": exc.limit,
                            "maximum": exc.maximum,
                            "path_digest": workspace_path_digest(rest),
                        },
                    )
                    raise
                except IntegrityError as exc:
                    self._session.rollback()
                    if not self._is_version_conflict(exc):
                        raise
                    self._telemetry.emit(
                        "workspace.version_conflict",
                        attributes={
                            "attempt": attempt + 1,
                            "path_digest": workspace_path_digest(rest),
                        },
                    )
                    if attempt + 1 == _VERSION_RETRIES:
                        raise WorkspaceVersionConflict(
                            f"Could not allocate Workspace version for {rest}"
                        ) from exc
                except Exception:
                    self._session.rollback()
                    raise
        raise WorkspaceVersionConflict(
            f"Could not allocate Workspace version for {rest}"
        )

    def _acquire_database_write_lock(self) -> None:
        """在 PostgreSQL 进程间串行化应用写入。"""
        bind = self._session.get_bind()
        if bind.dialect.name != "postgresql":
            return
        digest = hashlib.sha256(
            f"{self._account_id}:{self._app_id}".encode("ascii")
        ).digest()
        lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    @staticmethod
    def _is_version_conflict(exc: IntegrityError) -> bool:
        """识别 Workspace 路径版本唯一约束。"""
        message = str(getattr(exc, "orig", exc)).lower()
        return (
            "uq_workspace_files_path_version" in message
            or (
                "workspace_files.account_id" in message
                and "workspace_files.app_id" in message
                and "workspace_files.path" in message
                and "workspace_files.version" in message
            )
        )


__all__ = ["WorkspaceRepository", "WorkspaceVersionConflict"]
