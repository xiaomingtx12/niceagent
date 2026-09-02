"""Workspace 文件管理、授权和展示服务。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from injector import inject
from sqlalchemy import or_

from config import Config
from internal.core.agent_v2.entity.workspace_path import normalize_workspace_path
from internal.core.agent_v2.entity.workspace_scope import build_workspace_scope_id
from internal.core.agent_v2.entity.workspace_quota import AgentWorkspaceLimits
from internal.core.agent_v2.persistence.workspace_file_repository import (
    WorkspaceFileRepository,
    WorkspaceFileRevisionConflict,
)
from internal.entity.workspace_file_entity import (
    WorkspaceFilePreview,
    supports_text_preview,
)
from internal.exception import (
    FailException,
    WorkspacePathError,
    WorkspaceQuotaExceeded,
    WorkspaceStorageError,
)
from internal.model import WorkspaceFile
from pkg.sqlalchemy import SQLAlchemy

from .base_service import BaseService
from .workspace_file_storage_service import WorkspaceFileStorageService

MAX_WORKSPACE_PATH_LENGTH = 512


def _uuid(value: object, field: str) -> UUID:
    """把边界值规范为 UUID。"""
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise FailException(f"{field} 不是合法 UUID") from exc


def _escape_like(value: str) -> str:
    """转义 SQL LIKE 元字符。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _workspace_storage_path(path: str, scope_id: UUID) -> str:
    """把前端虚拟路径转换为稳定 Workspace 作用域下的物理路径。"""
    try:
        normalized = normalize_workspace_path(path)
    except WorkspacePathError as exc:
        raise FailException("Workspace 路径不合法") from exc
    if not normalized.startswith("/workspace/"):
        raise FailException("Workspace 路径必须以 /workspace/ 开头")
    relative = normalized.removeprefix("/workspace/")
    segments = relative.split("/")
    if (
        not relative
        or len(normalized) > MAX_WORKSPACE_PATH_LENGTH
        or any(part in {"", ".", "..", "~"} for part in segments)
    ):
        raise FailException("Workspace 路径不合法")
    return f"{scope_id}/{relative}"


@inject
@dataclass
class WorkspaceFileService(BaseService):
    """协调 Workspace 元数据、COS 内容和调用方访问权限。"""

    db: SQLAlchemy
    config: Config
    storage_service: WorkspaceFileStorageService

    def list_creator_files(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        prefix: str | None = None,
    ) -> list[WorkspaceFile]:
        """列出当前用户在 Agent 下共享 Workspace 的文件元数据。"""
        scope_id = self._resolve_workspace_scope(
            account_id=account_id,
            app_id=app_id,
        )
        query = self.db.session.query(WorkspaceFile).filter(
            WorkspaceFile.account_id == _uuid(account_id, "account_id"),
            WorkspaceFile.app_id == _uuid(app_id, "app_id"),
            WorkspaceFile.conversation_id == scope_id,
            WorkspaceFile.is_deleted.is_(False),
        )
        if prefix:
            normalized_prefix = prefix.strip()
            if normalized_prefix not in {"/workspace", "/workspace/"}:
                if not normalized_prefix.startswith("/workspace/"):
                    normalized_prefix = f"/workspace/{normalized_prefix.lstrip('/')}"
                storage_prefix = _workspace_storage_path(
                    normalized_prefix,
                    scope_id,
                )
                escaped = _escape_like(storage_prefix)
                query = query.filter(
                    or_(
                        WorkspaceFile.path == storage_prefix,
                        WorkspaceFile.path.like(f"{escaped}/%", escape="\\"),
                    )
                )
        return query.order_by(WorkspaceFile.updated_at.desc()).all()

    def list_shared_files(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        query: str | None = None,
    ) -> list[WorkspaceFile]:
        """列出账号在 Agent 下共享 Workspace 的文件。"""
        app_uuid = _uuid(app_id, "app_id")
        scope_id = self._resolve_workspace_scope(
            account_id=account_id,
            app_id=app_uuid,
        )
        file_query = self.db.session.query(WorkspaceFile).filter(
            WorkspaceFile.account_id == _uuid(account_id, "account_id"),
            WorkspaceFile.app_id == app_uuid,
            WorkspaceFile.conversation_id == scope_id,
            WorkspaceFile.is_deleted.is_(False),
        )
        query_text = (query or "").strip()
        if query_text:
            pattern = f"%{_escape_like(query_text)}%"
            file_query = file_query.filter(WorkspaceFile.path.ilike(pattern, escape="\\"))
        return file_query.order_by(WorkspaceFile.updated_at.desc()).all()

    def write_text(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        path: str,
        content: str,
        created_by: str | UUID,
        mime_type: str = "text/plain",
    ) -> WorkspaceFile:
        """解析用户 Workspace 作用域并把文本内容写入 COS。"""
        app_uuid = _uuid(app_id, "app_id")
        scope_id = self._resolve_workspace_scope(
            account_id=account_id,
            app_id=app_uuid,
        )
        repository = self._repository(
            account_id=_uuid(account_id, "account_id"),
            app_id=app_uuid,
            conversation_id=scope_id,
            created_by=_uuid(created_by, "created_by"),
        )
        try:
            return self.storage_service.write_text(
                repository,
                _workspace_storage_path(path, scope_id),
                content,
                mime_type=mime_type or "text/plain",
            )
        except (WorkspaceStorageError, WorkspaceQuotaExceeded) as exc:
            raise FailException(str(exc)) from exc
        except WorkspaceFileRevisionConflict as exc:
            raise FailException("Workspace 文件正在被修改，请重试") from exc

    def read_owned(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        path: str,
    ) -> WorkspaceFile:
        """读取当前用户在 Agent 共享 Workspace 中的文件元数据。"""
        scope_id = self._resolve_workspace_scope(
            account_id=account_id,
            app_id=app_id,
        )
        storage_path = _workspace_storage_path(path, scope_id)
        row = (
            self.db.session.query(WorkspaceFile)
            .filter(
                WorkspaceFile.account_id == _uuid(account_id, "account_id"),
                WorkspaceFile.app_id == _uuid(app_id, "app_id"),
                WorkspaceFile.conversation_id == scope_id,
                WorkspaceFile.path == storage_path,
                WorkspaceFile.is_deleted.is_(False),
            )
            .one_or_none()
        )
        if row is None:
            raise FailException("Workspace 文件不存在")
        return row

    def preview(self, row: WorkspaceFile) -> WorkspaceFilePreview:
        """返回受限文本内容或适合媒体展示的短期地址。"""
        try:
            if supports_text_preview(row):
                content = self.storage_service.read_bytes(
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
                download_url=self.get_download_url(row),
            )
        except WorkspaceStorageError as exc:
            raise FailException(str(exc)) from exc

    def get_download_url(self, row: WorkspaceFile) -> str:
        """生成当前授权文件的短期 COS 下载地址。"""
        try:
            return self.storage_service.get_download_url(
                row,
                expires_seconds=self.config.WORKSPACE_DOWNLOAD_URL_TTL_SECONDS,
            )
        except WorkspaceStorageError as exc:
            raise FailException(str(exc)) from exc

    def soft_delete(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        path: str,
    ) -> None:
        """提交删除状态并清理 COS 对象。"""
        row = self.read_owned(
            account_id=account_id,
            app_id=app_id,
            path=path,
        )
        repository = self._repository(
            account_id=_uuid(account_id, "account_id"),
            app_id=_uuid(app_id, "app_id"),
            conversation_id=row.conversation_id,
        )
        self.storage_service.delete(repository, row.path)

    def _repository(
        self,
        *,
        account_id: UUID,
        app_id: UUID,
        conversation_id: UUID,
        created_by: UUID | None = None,
    ) -> WorkspaceFileRepository:
        """构造绑定稳定 Workspace 作用域的元数据仓储。"""
        return WorkspaceFileRepository(
            session=self.db.session,
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation_id,
            created_by=created_by,
            limits=AgentWorkspaceLimits(
                max_bytes=self.config.WORKSPACE_MAX_BYTES,
                max_files=self.config.WORKSPACE_MAX_FILES,
            ),
        )

    @staticmethod
    def _resolve_workspace_scope(
        *,
        account_id: str | UUID,
        app_id: str | UUID,
    ) -> UUID:
        """解析账号级 Workspace 作用域；会话只负责线程状态。"""
        return build_workspace_scope_id(
            _uuid(account_id, "account_id"),
            _uuid(app_id, "app_id"),
        )

__all__ = ["WorkspaceFileService"]
