"""Workspace 文件业务服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from injector import inject

from config import Config
from internal.core.agent_v2.entity.workspace_path import (
    AgentWorkspacePathPolicy,
    WorkspacePathError,
)
from internal.core.agent_v2.entity.workspace_quota import (
    AgentWorkspaceLimits,
    WorkspaceQuotaExceeded,
    WorkspaceQuotaKind,
)
from internal.core.agent_v2.entity.workspace_scope import build_workspace_scope_id
from internal.entity.workspace_file_entity import (
    WorkspaceFilePreview,
    supports_text_preview,
)
from internal.exception import FailException, NotFoundException, ValidateErrorException
from internal.model import WorkspaceFile
from internal.repository import (
    WorkspaceFileRepository,
    WorkspaceFileRevisionConflict,
    WorkspaceFileStore,
    WorkspaceStorageError,
)
from pkg.sqlalchemy import SQLAlchemy


@inject
@dataclass
class WorkspaceFileService:
    """处理 Workspace 文件用例并转换可预期的业务失败。"""

    db: SQLAlchemy
    config: Config
    file_store: WorkspaceFileStore

    def list_files(
        self,
        *,
        account_id: UUID,
        app_id: UUID,
        prefix: str | None = None,
    ) -> list[WorkspaceFile]:
        """列出当前账号在 Agent 共享 Workspace 中的文件。"""
        repository, paths = self._workspace(account_id=account_id, app_id=app_id)
        storage_prefix = self._resolve_prefix(paths, prefix)
        prefixes = (storage_prefix,) if storage_prefix is not None else None
        return sorted(
            repository.list(prefixes),
            key=self._updated_at,
            reverse=True,
        )

    def search_files(
        self,
        *,
        account_id: UUID,
        app_id: UUID,
        query: str | None = None,
    ) -> list[WorkspaceFile]:
        """按虚拟路径搜索当前账号的共享 Workspace 文件。"""
        repository, _ = self._workspace(account_id=account_id, app_id=app_id)
        rows = repository.list()
        query_text = (query or "").strip().casefold()
        if query_text:
            rows = [row for row in rows if query_text in row.virtual_path.casefold()]
        return sorted(rows, key=self._updated_at, reverse=True)

    def write_text(
        self,
        *,
        account_id: UUID,
        app_id: UUID,
        path: str,
        content: str,
        created_by: UUID,
        mime_type: str = "text/plain",
    ) -> WorkspaceFile:
        """把文本写入当前账号的共享 Workspace。"""
        repository, paths = self._workspace(
            account_id=account_id,
            app_id=app_id,
            created_by=created_by,
        )
        storage_path = self._resolve_path(paths, path)
        try:
            return self.file_store.write_text(
                repository,
                storage_path,
                content,
                mime_type=mime_type or "text/plain",
            )
        except WorkspaceQuotaExceeded as exc:
            message = {
                WorkspaceQuotaKind.FILES: (
                    f"Workspace 文件数量不能超过 {exc.maximum}"
                ),
                WorkspaceQuotaKind.BYTES: (
                    f"Workspace 总容量不能超过 {exc.maximum} 字节"
                ),
            }.get(exc.kind, "Workspace 容量超出限制")
            raise ValidateErrorException(message) from exc
        except WorkspaceStorageError as exc:
            raise FailException("Workspace 文件存储失败，请稍后重试") from exc
        except WorkspaceFileRevisionConflict as exc:
            raise FailException("Workspace 文件正在被修改，请重试") from exc

    def preview(
        self,
        *,
        account_id: UUID,
        app_id: UUID,
        path: str,
    ) -> WorkspaceFilePreview:
        """读取授权文件并返回受限文本内容或短期地址。"""
        row = self._get_file(account_id=account_id, app_id=app_id, path=path)
        try:
            if supports_text_preview(row):
                content = self.file_store.read_bytes(
                    row,
                    max_bytes=self.config.WORKSPACE_PREVIEW_MAX_BYTES,
                )
                return WorkspaceFilePreview(
                    file=row,
                    content=content.decode("utf-8", errors="replace"),
                    truncated=row.size_bytes > len(content),
                )
            return WorkspaceFilePreview(
                file=row,
                download_url=self._get_download_url(row),
            )
        except WorkspaceStorageError as exc:
            raise FailException("Workspace 文件读取失败，请稍后重试") from exc

    def get_download_url(
        self,
        *,
        account_id: UUID,
        app_id: UUID,
        path: str,
    ) -> str:
        """读取授权文件并生成短期下载地址。"""
        row = self._get_file(account_id=account_id, app_id=app_id, path=path)
        try:
            return self._get_download_url(row)
        except WorkspaceStorageError as exc:
            raise FailException("Workspace 下载地址生成失败，请稍后重试") from exc

    def delete(
        self,
        *,
        account_id: UUID,
        app_id: UUID,
        path: str,
    ) -> None:
        """删除当前账号共享 Workspace 中的文件。"""
        row = self._get_file(account_id=account_id, app_id=app_id, path=path)
        repository, _ = self._workspace(account_id=account_id, app_id=app_id)
        try:
            self.file_store.delete(repository, row.path)
        except WorkspaceStorageError as exc:
            raise FailException("Workspace 文件删除失败，请稍后重试") from exc

    def _get_file(
        self,
        *,
        account_id: UUID,
        app_id: UUID,
        path: str,
    ) -> WorkspaceFile:
        """按授权作用域读取文件，不向调用方暴露物理路径。"""
        repository, paths = self._workspace(account_id=account_id, app_id=app_id)
        row = repository.fetch(self._resolve_path(paths, path))
        if row is None:
            raise NotFoundException("Workspace 文件不存在")
        return row

    def _get_download_url(self, row: WorkspaceFile) -> str:
        """为已经过授权的文件生成短期下载地址。"""
        return self.file_store.get_download_url(
            row,
            expires_seconds=self.config.WORKSPACE_DOWNLOAD_URL_TTL_SECONDS,
        )

    def _workspace(
        self,
        *,
        account_id: UUID,
        app_id: UUID,
        created_by: UUID | None = None,
    ) -> tuple[WorkspaceFileRepository, AgentWorkspacePathPolicy]:
        """创建绑定稳定 Workspace 作用域的仓储和路径策略。"""
        scope_id = build_workspace_scope_id(account_id, app_id)
        return (
            WorkspaceFileRepository(
                session=self.db.session,
                account_id=account_id,
                app_id=app_id,
                scope_id=scope_id,
                created_by=created_by,
                limits=AgentWorkspaceLimits(
                    max_bytes=self.config.WORKSPACE_MAX_BYTES,
                    max_files=self.config.WORKSPACE_MAX_FILES,
                ),
            ),
            AgentWorkspacePathPolicy(scope_id=scope_id),
        )

    @staticmethod
    def _resolve_path(paths: AgentWorkspacePathPolicy, path: str) -> str:
        """把领域路径错误转换为接口可识别的校验异常。"""
        try:
            return paths.resolve_file(path)
        except WorkspacePathError as exc:
            raise ValidateErrorException("Workspace 路径不合法") from exc

    @staticmethod
    def _resolve_prefix(
        paths: AgentWorkspacePathPolicy,
        prefix: str | None,
    ) -> str | None:
        """把可选虚拟目录前缀转换为物理存储前缀。"""
        normalized = (prefix or "").strip()
        if normalized in {"", "/workspace", "/workspace/"}:
            return None
        if not normalized.startswith("/workspace/"):
            normalized = f"/workspace/{normalized.lstrip('/')}"
        try:
            return paths.resolve_prefix(normalized)
        except WorkspacePathError as exc:
            raise ValidateErrorException("Workspace 路径不合法") from exc

    @staticmethod
    def _updated_at(row: WorkspaceFile) -> datetime:
        """为展示排序提供稳定的更新时间键。"""
        return row.updated_at or row.created_at


__all__ = ["WorkspaceFileService"]
