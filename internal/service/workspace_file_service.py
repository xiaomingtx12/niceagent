"""Workspace 文件管理、授权和展示服务。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from injector import inject
from sqlalchemy import or_

from config import Config
from internal.core.agent_v2.entity.workspace_path import normalize_workspace_path
from internal.core.agent_v2.entity.workspace_quota import AgentWorkspaceLimits
from internal.core.agent_v2.persistence.workspace_file_repository import (
    WorkspaceFileRepository,
    WorkspaceFileRevisionConflict,
)
from internal.entity.conversation_entity import InvokeFrom
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
from internal.model import Conversation, WorkspaceFile
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


def _conversation_storage_path(path: str, conversation_id: UUID) -> str:
    """把前端虚拟路径转换为会话隔离的物理元数据路径。"""
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
    return f"{conversation_id}/{relative}"


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
        conversation_id: str | UUID,
        prefix: str | None = None,
    ) -> list[WorkspaceFile]:
        """列出创建者当前会话内的文件元数据。"""
        conversation_uuid = self._require_conversation(app_id, conversation_id)
        query = self.db.session.query(WorkspaceFile).filter(
            WorkspaceFile.account_id == _uuid(account_id, "account_id"),
            WorkspaceFile.app_id == _uuid(app_id, "app_id"),
            WorkspaceFile.conversation_id == conversation_uuid,
            WorkspaceFile.is_deleted.is_(False),
        )
        if prefix:
            storage_prefix = _conversation_storage_path(prefix, conversation_uuid)
            escaped = _escape_like(storage_prefix)
            query = query.filter(
                or_(
                    WorkspaceFile.path == storage_prefix,
                    WorkspaceFile.path.like(f"{escaped}/%", escape="\\"),
                )
            )
        return query.order_by(WorkspaceFile.updated_at.desc()).all()

    def list_end_user_all_files(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        owner_ids: list[str | UUID],
        query: str | None = None,
    ) -> list[WorkspaceFile]:
        """列出终端用户拥有的 WebApp 会话文件。"""
        app_uuid = _uuid(app_id, "app_id")
        owner_uuids = [_uuid(value, "owner_id") for value in owner_ids]
        file_query = (
            self.db.session.query(WorkspaceFile)
            .join(Conversation, Conversation.id == WorkspaceFile.conversation_id)
            .filter(
                WorkspaceFile.account_id == _uuid(account_id, "account_id"),
                WorkspaceFile.app_id == app_uuid,
                WorkspaceFile.is_deleted.is_(False),
                Conversation.app_id == app_uuid,
                Conversation.invoke_from == InvokeFrom.WEB_APP,
                Conversation.created_by.in_(owner_uuids),
                Conversation.is_deleted.is_(False),
            )
        )
        query_text = (query or "").strip()
        if query_text:
            pattern = f"%{_escape_like(query_text)}%"
            file_query = file_query.filter(
                or_(
                    WorkspaceFile.path.ilike(pattern, escape="\\"),
                    Conversation.name.ilike(pattern, escape="\\"),
                )
            )
        return file_query.order_by(WorkspaceFile.updated_at.desc()).all()

    def conversation_names(
        self,
        *,
        app_id: str | UUID,
        conversation_ids: list[str | UUID],
    ) -> dict[str, str]:
        """返回文件列表所需的会话名称映射。"""
        if not conversation_ids:
            return {}
        rows = (
            self.db.session.query(Conversation.id, Conversation.name)
            .filter(
                Conversation.app_id == _uuid(app_id, "app_id"),
                Conversation.id.in_(
                    [_uuid(value, "conversation_id") for value in conversation_ids]
                ),
            )
            .all()
        )
        return {str(item_id): name or "未命名会话" for item_id, name in rows}

    def write_text(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        conversation_id: str | UUID,
        path: str,
        content: str,
        created_by: str | UUID,
        mime_type: str = "text/plain",
    ) -> WorkspaceFile:
        """校验会话归属并把文本内容写入 COS。"""
        app_uuid = _uuid(app_id, "app_id")
        conversation_uuid = self._require_conversation(app_uuid, conversation_id)
        repository = self._repository(
            account_id=_uuid(account_id, "account_id"),
            app_id=app_uuid,
            conversation_id=conversation_uuid,
            created_by=_uuid(created_by, "created_by"),
        )
        try:
            return self.storage_service.write_text(
                repository,
                _conversation_storage_path(path, conversation_uuid),
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
        conversation_id: str | UUID,
        end_user_conversation_id: str | UUID | None = None,
    ) -> WorkspaceFile:
        """读取当前会话拥有的文件元数据。"""
        expected = end_user_conversation_id or conversation_id
        conversation_uuid = self._require_conversation(app_id, expected)
        storage_path = _conversation_storage_path(path, conversation_uuid)
        row = (
            self.db.session.query(WorkspaceFile)
            .filter(
                WorkspaceFile.account_id == _uuid(account_id, "account_id"),
                WorkspaceFile.app_id == _uuid(app_id, "app_id"),
                WorkspaceFile.conversation_id == conversation_uuid,
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
        conversation_id: str | UUID,
    ) -> None:
        """提交删除状态并清理 COS 对象。"""
        row = self.read_owned(
            account_id=account_id,
            app_id=app_id,
            path=path,
            conversation_id=conversation_id,
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
        """构造绑定当前业务会话的元数据仓储。"""
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

    def _require_conversation(
        self,
        app_id: str | UUID,
        conversation_id: str | UUID,
    ) -> UUID:
        """确认会话存在且属于当前应用。"""
        conversation_uuid = _uuid(conversation_id, "conversation_id")
        exists = (
            self.db.session.query(Conversation.id)
            .filter(
                Conversation.id == conversation_uuid,
                Conversation.app_id == _uuid(app_id, "app_id"),
                Conversation.is_deleted.is_(False),
            )
            .one_or_none()
        )
        if exists is None:
            raise FailException("Workspace 会话不存在或不属于当前应用")
        return conversation_uuid


__all__ = ["WorkspaceFileService"]
