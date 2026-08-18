"""Workspace file management and visibility service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from config.config import Config
from injector import inject
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
from internal.exception import FailException
from internal.model import Conversation, WorkspaceFile
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService

MAX_WORKSPACE_PATH_LENGTH = 512


def _uuid(value, field: str) -> UUID:
    """把字段值规范为 UUID，非法时抛业务异常。"""
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise FailException(f"{field} 不是合法 UUID") from exc


def normalize_workspace_relative_path(
    path: str,
    *,
    conversation_id: UUID | None,
) -> str:
    """Validate a stored relative path and enforce its visibility scope."""
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
    if contains_reserved_segment(normalized):
        raise FailException("Workspace 路径属于保留命名空间")
    allowed_root = "seed" if conversation_id is None else str(conversation_id)
    if segments[0] != allowed_root:
        raise FailException(f"Workspace 路径必须位于 {allowed_root}/ 下")
    return "/".join(segments)


@inject
@dataclass
class WorkspaceFileService(BaseService):
    """Versioned file CRUD with creator/end-user visibility rules."""

    db: SQLAlchemy

    def list_creator_files(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        conversation_id: str | UUID | None = None,
        prefix: str | None = None,
    ) -> list[WorkspaceFile]:
        """列出 Creator 作用域内最新的可见文件。"""
        query = self.db.session.query(WorkspaceFile).filter(
            WorkspaceFile.account_id == _uuid(account_id, "account_id"),
            WorkspaceFile.app_id == _uuid(app_id, "app_id"),
        )
        if conversation_id is not None:
            query = query.filter(
                WorkspaceFile.conversation_id
                == _uuid(conversation_id, "conversation_id")
            )
        if prefix:
            normalized = prefix.strip("/")
            query = query.filter(WorkspaceFile.path.like(f"{normalized}%"))
        return [
            row
            for row in self._latest(query.all())
            if not row.is_deleted and not contains_reserved_segment(row.path)
        ]

    def list_end_user_files(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        conversation_id: str | UUID,
        prefix: str | None = None,
    ) -> list[WorkspaceFile]:
        """列出终端用户会话下最新的文件。"""
        conversation_uuid = _uuid(conversation_id, "conversation_id")
        return [
            row
            for row in self.list_creator_files(
                account_id=account_id,
                app_id=app_id,
                conversation_id=conversation_uuid,
                prefix=prefix,
            )
            if row.conversation_id == conversation_uuid
        ]

    def write_text(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        conversation_id: str | UUID | None,
        path: str,
        content: str,
        created_by: str | UUID,
        mime_type: str = "text/plain",
    ) -> WorkspaceFile:
        """校验作用域并追加经配额检查的文本版本。"""
        account_uuid = _uuid(account_id, "account_id")
        app_uuid = _uuid(app_id, "app_id")
        conversation_uuid = (
            _uuid(conversation_id, "conversation_id")
            if conversation_id is not None
            else None
        )
        if conversation_uuid is not None:
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
        relative = normalize_workspace_relative_path(
            path,
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
        end_user_conversation_id: str | UUID | None = None,
    ) -> WorkspaceFile:
        """在校验归属后读取最新可见文件。"""
        normalized_path = path.strip("/")
        if contains_reserved_segment(normalized_path):
            raise FailException("Workspace 文件不存在")
        row = self._latest_one(
            _uuid(account_id, "account_id"),
            _uuid(app_id, "app_id"),
            normalized_path,
        )
        if row is None or row.is_deleted:
            raise FailException("Workspace 文件不存在")
        if end_user_conversation_id is not None:
            conversation_uuid = _uuid(
                end_user_conversation_id,
                "conversation_id",
            )
            if row.conversation_id != conversation_uuid:
                raise FailException("无权访问其他会话的 Workspace 文件")
        return row

    def soft_delete(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        path: str,
    ) -> None:
        """Hide the latest owned file version from subsequent reads."""
        row = self.read_owned(
            account_id=account_id,
            app_id=app_id,
            path=path,
        )
        row.is_deleted = True
        row.updated_at = datetime.now(timezone.utc)
        self.db.session.commit()

    def _latest_one(
        self,
        account_id: UUID,
        app_id: UUID,
        path: str,
    ) -> WorkspaceFile | None:
        """Return the newest version for one scoped storage path."""
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
        """Collapse version rows to the latest row per path."""
        rows.sort(key=lambda row: (row.path, -row.version))
        latest: dict[str, WorkspaceFile] = {}
        for row in rows:
            latest.setdefault(row.path, row)
        return sorted(latest.values(), key=lambda row: row.updated_at, reverse=True)


__all__ = ["WorkspaceFileService", "normalize_workspace_relative_path"]
