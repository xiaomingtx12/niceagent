"""Workspace 文件元数据与对象内容的一致性存储。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from injector import inject
from sqlalchemy.orm import Session

from internal.core.agent_v2.assembly.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
    build_path_digest,
)
from internal.extension.logging_extension import logger
from internal.model import WorkspaceFile

from .cos_object_storage_repository import (
    CosObjectStorageRepository,
    ObjectStorageError,
)
from .workspace_file_repository import (
    WorkspaceFileRepository,
    WorkspaceStorageError,
)


@inject
@dataclass
class WorkspaceFileStore:
    """封装 Workspace 元数据、对象内容、补偿和遥测细节。"""

    object_storage: CosObjectStorageRepository

    def write_bytes(
        self,
        repository: WorkspaceFileRepository,
        path: str,
        content: bytes,
        *,
        mime_type: str = "application/octet-stream",
        enforce_quota: bool = True,
        telemetry: AgentTelemetry | None = None,
    ) -> WorkspaceFile:
        """先写唯一对象，再原子更新元数据并清理旧对象。"""
        encoded = bytes(content)
        digest = sha256(encoded).hexdigest()
        current = repository.fetch(path)
        if (
            current is not None
            and current.content_sha256 == digest
            and current.mime_type == mime_type
        ):
            return current

        object_key = self._build_object_key(repository, path)
        try:
            self.object_storage.put_bytes(
                object_key,
                encoded,
                content_type=mime_type,
            )
        except ObjectStorageError as exc:
            raise WorkspaceStorageError(str(exc)) from exc
        try:
            row, old_object_key = repository.save_metadata(
                path=path,
                object_key=object_key,
                mime_type=mime_type,
                size_bytes=len(encoded),
                content_sha256=digest,
                enforce_quota=enforce_quota,
            )
        except Exception:
            self._delete_best_effort(object_key)
            raise

        if old_object_key and old_object_key != object_key:
            self._delete_best_effort(old_object_key)
        (telemetry or NOOP_AGENT_TELEMETRY).emit(
            "workspace.write",
            value=float(len(encoded)),
            attributes={
                "path_digest": build_path_digest(path),
                "size_bytes": len(encoded),
                "revision": row.revision,
            },
        )
        return row

    def write_text(
        self,
        repository: WorkspaceFileRepository,
        path: str,
        content: str,
        *,
        mime_type: str = "text/plain",
        enforce_quota: bool = True,
        telemetry: AgentTelemetry | None = None,
    ) -> WorkspaceFile:
        """按 UTF-8 写入文本文件。"""
        return self.write_bytes(
            repository,
            path,
            content.encode("utf-8"),
            mime_type=mime_type,
            enforce_quota=enforce_quota,
            telemetry=telemetry,
        )

    def read_bytes(
        self,
        row: WorkspaceFile,
        *,
        max_bytes: int | None = None,
    ) -> bytes:
        """按元数据中的对象 key 读取内容。"""
        if row.is_deleted:
            raise WorkspaceStorageError("Workspace 文件已删除")
        try:
            return self.object_storage.get_bytes(row.object_key, max_bytes=max_bytes)
        except ObjectStorageError as exc:
            raise WorkspaceStorageError(str(exc)) from exc

    def delete(
        self,
        repository: WorkspaceFileRepository,
        path: str,
    ) -> None:
        """先提交删除状态，再清理不再可见的对象。"""
        object_key = repository.mark_deleted(path)
        if object_key:
            self._delete_best_effort(object_key)

    def delete_app_files(
        self,
        session: Session,
        *,
        account_id: UUID,
        app_id: UUID,
    ) -> None:
        """提交应用下全部文件的删除状态，再尽力清理对象。"""
        rows = (
            session.query(WorkspaceFile)
            .filter(
                WorkspaceFile.account_id == account_id,
                WorkspaceFile.app_id == app_id,
                WorkspaceFile.is_deleted.is_(False),
            )
            .all()
        )
        if not rows:
            return
        object_keys = [row.object_key for row in rows if row.object_key]
        try:
            now = datetime.now(timezone.utc)
            for row in rows:
                row.is_deleted = True
                row.revision += 1
                row.updated_at = now
            session.commit()
        except Exception:
            session.rollback()
            raise
        for object_key in object_keys:
            self._delete_best_effort(object_key)

    def get_download_url(
        self,
        row: WorkspaceFile,
        *,
        expires_seconds: int,
    ) -> str:
        """为已授权文件生成短期下载地址。"""
        if row.is_deleted:
            raise WorkspaceStorageError("Workspace 文件已删除")
        try:
            return self.object_storage.get_download_url(
                row.object_key,
                expires_seconds=expires_seconds,
            )
        except ObjectStorageError as exc:
            raise WorkspaceStorageError(str(exc)) from exc

    @staticmethod
    def _build_object_key(
        repository: WorkspaceFileRepository,
        path: str,
    ) -> str:
        """生成不复用旧内容的不可变对象 key。"""
        suffix = PurePosixPath(path).suffix
        return (
            "agent-workspaces/"
            f"{repository.account_id}/{repository.app_id}/"
            f"{repository.scope_id}/{uuid4().hex}{suffix}"
        )

    def _delete_best_effort(self, object_key: str) -> None:
        """补偿清理失败只记录摘要，不覆盖已完成的主事务。"""
        try:
            self.object_storage.delete(object_key)
        except ObjectStorageError:
            logger.exception(
                "清理 Workspace COS 对象失败: object_key_digest=%s",
                sha256(object_key.encode("utf-8")).hexdigest()[:16],
            )


__all__ = ["WorkspaceFileStore"]
