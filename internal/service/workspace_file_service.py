"""Workspace file management and visibility service."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from uuid import UUID, uuid4

from internal.exception import FailException
from internal.model import Conversation, WorkspaceFile
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService

MAX_WORKSPACE_PATH_LENGTH = 512


def _uuid(value, field: str) -> UUID:
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
    allowed_root = "seed" if conversation_id is None else str(conversation_id)
    if segments[0] != allowed_root:
        raise FailException(f"Workspace 路径必须位于 {allowed_root}/ 下")
    return "/".join(segments)


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
            row for row in self._latest(query.all()) if not row.is_deleted
        ]

    def list_end_user_files(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        conversation_id: str | UUID,
        prefix: str | None = None,
    ) -> list[WorkspaceFile]:
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
        latest = self._latest_one(account_uuid, app_uuid, relative)
        encoded = content.encode("utf-8")
        row = WorkspaceFile(
            id=uuid4(),
            account_id=account_uuid,
            app_id=app_uuid,
            conversation_id=conversation_uuid,
            path=relative,
            version=(latest.version + 1) if latest else 1,
            storage_backend="db",
            mime_type=mime_type,
            size_bytes=len(encoded),
            content_sha256=hashlib.sha256(encoded).hexdigest(),
            content=encoded,
            created_by=_uuid(created_by, "created_by"),
            is_deleted=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.db.session.add(row)
        self.db.session.commit()
        return row

    def read_owned(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        path: str,
        end_user_conversation_id: str | UUID | None = None,
    ) -> WorkspaceFile:
        row = self._latest_one(
            _uuid(account_id, "account_id"),
            _uuid(app_id, "app_id"),
            path.strip("/"),
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
        rows.sort(key=lambda row: (row.path, -row.version))
        latest: dict[str, WorkspaceFile] = {}
        for row in rows:
            latest.setdefault(row.path, row)
        return sorted(latest.values(), key=lambda row: row.updated_at, reverse=True)


__all__ = ["WorkspaceFileService", "normalize_workspace_relative_path"]
