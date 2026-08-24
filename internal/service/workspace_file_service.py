"""Workspace 文件管理和可见性校验服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from config.config import Config
from injector import inject
from sqlalchemy import or_
from internal.core.agent_v2.backends.workspace_path import (
    contains_reserved_segment,
)
from internal.core.agent_v2.backends.workspace_quota import (
    WorkspaceLimits,
    WorkspaceQuotaExceeded,
)
from internal.core.agent_v2.persistence.workspace import (
    WorkspaceRepository,
    WorkspaceVersionConflict,
)
from internal.core.agent_v2.runtime.telemetry import (
    AgentTelemetry,
    LoggingAgentTelemetrySink,
)
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import FailException
from internal.model import Conversation, WorkspaceFile
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService

MAX_WORKSPACE_PATH_LENGTH = 512


def _like_pattern(value: str) -> str:
    """在拼接 SQL LIKE 条件前转义用户输入。"""
    escaped = (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _workspace_path_like_patterns(value: str) -> list[str]:
    """为物理存储路径和虚拟 Workspace 路径构造查询模式。"""
    normalized = value.strip().replace("\\", "/")
    raw_pattern = _like_pattern(normalized)
    patterns = [raw_pattern]
    for prefix in ("/workspace/", "workspace/"):
        if normalized.lower().startswith(prefix):
            relative = normalized[len(prefix):].strip("/")
            if relative:
                patterns.append(_like_pattern(f"/{relative}"))
            break
    return patterns


def _uuid(value, field: str) -> UUID:
    """把字段值规范为 UUID，非法时抛业务异常。"""
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise FailException(f"{field} 不是合法 UUID") from exc


def normalize_workspace_relative_path(
    path: str,
    *,
    conversation_id: UUID,
) -> str:
    """校验物理相对路径，并强制绑定当前会话的可见范围。"""
    normalized = (path or "").strip().replace("\\", "/").strip("/")
    segments = normalized.split("/") if normalized else []
    if (
        not segments
        or len(normalized) > MAX_WORKSPACE_PATH_LENGTH
        or "\x00" in normalized
        or any(
            part in {"", ".", "..", "~"}
            or any(ord(character) < 32 for character in part)
            for part in segments
        )
    ):
        raise FailException("Workspace 路径不合法")
    if conversation_id is None:
        raise FailException("Workspace 必须绑定当前会话")
    if contains_reserved_segment(normalized):
        raise FailException("Workspace 路径属于保留命名空间")
    allowed_root = str(conversation_id)
    if segments[0] != allowed_root:
        raise FailException(f"Workspace 路径必须位于 {allowed_root}/ 下")
    return "/".join(segments)


def _conversation_storage_path(path: str, conversation_id: UUID) -> str:
    """把 /workspace/ 虚拟路径转换为会话物理路径。"""
    normalized = (path or "").strip().replace("\\", "/").strip("/")
    if not normalized.startswith("workspace/"):
        raise FailException("Workspace 路径必须以 /workspace/ 开头")
    relative = normalized.removeprefix("workspace/")
    if not relative:
        raise FailException("Workspace 目录不能作为文件")
    return normalize_workspace_relative_path(
        f"{conversation_id}/{relative}",
        conversation_id=conversation_id,
    )


@inject
@dataclass
class WorkspaceFileService(BaseService):
    """管理 Workspace 文件版本，并执行创建者和终端用户可见性校验。"""

    db: SQLAlchemy

    def list_creator_files(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        conversation_id: str | UUID | None = None,
        prefix: str | None = None,
    ) -> list[WorkspaceFile]:
        """列出创建者作用域内最新的可见文件。"""
        if conversation_id is None:
            return []
        conversation_uuid = _uuid(conversation_id, "conversation_id")
        query = self.db.session.query(WorkspaceFile).filter(
            WorkspaceFile.account_id == _uuid(account_id, "account_id"),
            WorkspaceFile.app_id == _uuid(app_id, "app_id"),
            WorkspaceFile.conversation_id == conversation_uuid,
        )
        if prefix:
            normalized = prefix.strip("/")
            if normalized != "workspace":
                storage_prefix = _conversation_storage_path(
                    normalized,
                    conversation_uuid,
                )
                query = query.filter(
                    WorkspaceFile.path.like(f"{storage_prefix}%")
                )
        return [
            row
            for row in self._latest(query.all())
            if not row.is_deleted and not contains_reserved_segment(row.path)
        ]

    def list_end_user_all_files(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        owner_ids: list[str | UUID],
        query: str | None = None,
    ) -> list[WorkspaceFile]:
        """列出终端用户在多个 WebApp 会话中的 Workspace 文件。"""
        account_uuid = _uuid(account_id, "account_id")
        app_uuid = _uuid(app_id, "app_id")
        owner_uuids = [_uuid(value, "owner_id") for value in owner_ids]
        conversation_query = (
            self.db.session.query(Conversation.id)
            .filter(
                Conversation.app_id == app_uuid,
                Conversation.invoke_from == InvokeFrom.WEB_APP,
                Conversation.created_by.in_(owner_uuids),
                Conversation.is_deleted.is_(False),
            )
        )
        query_text = (query or "").strip()
        file_query = self.db.session.query(WorkspaceFile).filter(
            WorkspaceFile.account_id == account_uuid,
            WorkspaceFile.app_id == app_uuid,
            WorkspaceFile.conversation_id.in_(conversation_query),
        )
        if query_text:
            path_patterns = _workspace_path_like_patterns(query_text)
            path_filters = [
                WorkspaceFile.path.ilike(pattern, escape="\\")
                for pattern in path_patterns
            ]
            file_query = file_query.join(
                Conversation,
                Conversation.id == WorkspaceFile.conversation_id,
            ).filter(
                or_(
                    *path_filters,
                    Conversation.name.ilike(
                        _like_pattern(query_text),
                        escape="\\",
                    ),
                )
            )
        return [
            row
            for row in self._latest(file_query.all())
            if not row.is_deleted and not contains_reserved_segment(row.path)
        ]

    def conversation_names(
        self,
        *,
        app_id: str | UUID,
        conversation_ids: list[str | UUID],
    ) -> dict[str, str]:
        """返回 Workspace 文件列表所需的会话名称映射。"""
        if not conversation_ids:
            return {}
        app_uuid = _uuid(app_id, "app_id")
        conversation_uuids = [
            _uuid(value, "conversation_id") for value in conversation_ids
        ]
        rows = (
            self.db.session.query(Conversation.id, Conversation.name)
            .filter(
                Conversation.app_id == app_uuid,
                Conversation.id.in_(conversation_uuids),
            )
            .all()
        )
        return {str(conversation_id): name or "未命名会话" for conversation_id, name in rows}

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
        """校验作用域并追加经配额检查的文本版本。"""
        # 1.校验租户、应用和会话归属。
        account_uuid = _uuid(account_id, "account_id")
        app_uuid = _uuid(app_id, "app_id")
        conversation_uuid = _uuid(conversation_id, "conversation_id")
        conversation = (
            self.db.session.query(Conversation.id)
            .filter(
                Conversation.id == conversation_uuid,
                Conversation.app_id == app_uuid,
                Conversation.is_deleted.is_(False),
            )
            .one_or_none()
        )
        if conversation is None:
            raise FailException("Workspace 会话不存在或不属于当前应用")
        # 2.把虚拟路径转换为会话隔离的物理存储路径。
        storage_path = _conversation_storage_path(path, conversation_uuid)
        relative = normalize_workspace_relative_path(
            storage_path,
            conversation_id=conversation_uuid,
        )
        config = Config()
        repository = WorkspaceRepository(
            session=self.db.session,
            account_id=account_uuid,
            app_id=app_uuid,
            conversation_id=conversation_uuid,
            created_by=_uuid(created_by, "created_by"),
            limits=WorkspaceLimits(
                max_bytes=config.WORKSPACE_MAX_BYTES,
                max_files=config.WORKSPACE_MAX_FILES,
            ),
            telemetry=AgentTelemetry(
                LoggingAgentTelemetrySink(),
                attributes={
                    "account_id": str(account_uuid),
                    "app_id": str(app_uuid),
                    "conversation_id": (
                        str(conversation_uuid) if conversation_uuid else None
                    ),
                    "actor_id": str(created_by),
                },
            ),
        )
        try:
            # 3.由仓储层执行版本追加、配额检查和遥测记录。
            return repository.write_text(
                relative,
                content,
                mime_type=mime_type,
            )
        except WorkspaceQuotaExceeded as exc:
            raise FailException(str(exc)) from exc
        except WorkspaceVersionConflict as exc:
            raise FailException("Workspace 文件版本冲突，请重试") from exc

    def read_owned(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        path: str,
        conversation_id: str | UUID,
        end_user_conversation_id: str | UUID | None = None,
    ) -> WorkspaceFile:
        """在校验归属后读取最新可见文件。"""
        # 1.拒绝保留命名空间，并解析调用方作用域。
        normalized_path = path.strip("/")
        if contains_reserved_segment(normalized_path):
            raise FailException("Workspace 文件不存在")
        account_uuid = _uuid(account_id, "account_id")
        app_uuid = _uuid(app_id, "app_id")
        expected_conversation_id = (
            end_user_conversation_id
            if end_user_conversation_id is not None
            else conversation_id
        )
        conversation_uuid = _uuid(expected_conversation_id, "conversation_id")
        storage_path = _conversation_storage_path(
            normalized_path,
            conversation_uuid,
        )
        row = self._latest_one(account_uuid, app_uuid, storage_path)
        # 2.只返回当前会话可见且未删除的最新版本。
        if row is None or row.is_deleted:
            raise FailException("Workspace 文件不存在")
        if row.conversation_id != conversation_uuid:
            raise FailException("无权访问其他会话的 Workspace 文件")
        return row

    def soft_delete(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        path: str,
        conversation_id: str | UUID,
    ) -> None:
        """软删除当前会话拥有的最新 Workspace 文件版本。"""
        # 1.复用读取权限校验，定位当前会话拥有的最新版本。
        row = self.read_owned(
            account_id=account_id,
            app_id=app_id,
            path=path,
            conversation_id=conversation_id,
        )
        row.is_deleted = True
        row.updated_at = datetime.now(timezone.utc)
        # 2.软删除文件版本，保留历史记录和版本链。
        self.db.session.commit()

    def _latest_one(
        self,
        account_id: UUID,
        app_id: UUID,
        path: str,
    ) -> WorkspaceFile | None:
        """读取指定租户和应用路径下的最新文件版本。"""
        return (
            self.db.session.query(WorkspaceFile)
            .filter(
                WorkspaceFile.account_id == account_id,
                WorkspaceFile.app_id == app_id,
                WorkspaceFile.path == path,
            )
            .order_by(WorkspaceFile.version.desc())
            .first()
        )

    @staticmethod
    def _latest(rows: list[WorkspaceFile]) -> list[WorkspaceFile]:
        """按路径折叠多个版本，只保留每个路径的最新记录。"""
        rows.sort(key=lambda row: (row.path, -row.version))
        latest: dict[str, WorkspaceFile] = {}
        for row in rows:
            latest.setdefault(row.path, row)
        return sorted(latest.values(), key=lambda row: row.updated_at, reverse=True)


__all__ = ["WorkspaceFileService", "normalize_workspace_relative_path"]
