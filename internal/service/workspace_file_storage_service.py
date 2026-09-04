"""Workspace 文件内容与元数据的一致性协调服务。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath
from uuid import uuid4

from injector import inject

from internal.core.agent_v2.entity.workspace_path import build_workspace_path_digest
from internal.core.agent_v2.persistence.workspace_file_repository import (
    WorkspaceFileRepository,
    WorkspaceStorageError,
)
from internal.core.agent_v2.assembly.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.model import WorkspaceFile
from internal.extension.logging_extension import logger
from internal.repository import CosObjectStorageRepository, ObjectStorageError

@inject
@dataclass
class WorkspaceFileStorageService:
    """协调 Workspace 文件的对象存储内容和 SQL 元数据。

    本服务不是普通附件 COS 门面，而是 Workspace 持久化协调器：负责 COS
    对象、WorkspaceFile 元数据、配额和失败补偿。Agent Core 直接使用这个
    具体服务，业务层的普通附件操作仍由 ``CosService`` 负责。
    """

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
        """先写唯一 COS 对象，再原子更新当前元数据。"""
        encoded = bytes(content)
        digest = sha256(encoded).hexdigest()
        current = repository.fetch(path)
        if current is not None and current.content_sha256 == digest:
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
                "path_digest": build_workspace_path_digest(path),
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
            raise ObjectStorageError("Workspace 文件已删除")
        try:
            return self.object_storage.get_bytes(row.object_key, max_bytes=max_bytes)
        except ObjectStorageError as exc:
            raise WorkspaceStorageError(str(exc)) from exc

    def delete(
        self,
        repository: WorkspaceFileRepository,
        path: str,
    ) -> None:
        """先提交删除状态，再清理不再可见的 COS 对象。"""
        object_key = repository.mark_deleted(path)
        if object_key:
            self._delete_best_effort(object_key)

    def delete_objects(self, rows: list[WorkspaceFile]) -> None:
        """尽力清理一批已提交删除状态的 COS 对象。"""
        for row in rows:
            if row.object_key:
                self._delete_best_effort(row.object_key)

    def get_download_url(
        self,
        row: WorkspaceFile,
        *,
        expires_seconds: int,
    ) -> str:
        """为已授权文件生成短期下载地址。"""
        if row.is_deleted:
            raise ObjectStorageError("Workspace 文件已删除")
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
        """生成不复用旧内容的不可变 COS 对象 key。"""
        suffix = PurePosixPath(path).suffix
        return (
            "agent-workspaces/"
            f"{repository.account_id}/{repository.app_id}/"
            f"{repository.conversation_id}/{uuid4().hex}{suffix}"
        )

    def _delete_best_effort(self, object_key: str) -> None:
        """补偿清理失败只记录 key，不覆盖已完成的主事务。"""
        try:
            self.object_storage.delete(object_key)
        except ObjectStorageError:
            logger.exception("清理 Workspace COS 对象失败: %s", object_key)


__all__ = ["WorkspaceFileStorageService"]
