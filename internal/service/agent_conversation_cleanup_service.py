"""清理 Agent 会话派生状态。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from injector import inject

from config.config import Config
from internal.core.agent_v2.backends.sandbox import build_stable_sandbox_name
from internal.core.agent_v2.persistence.checkpoint import (
    build_thread_id,
    get_configured_checkpointer,
)
from internal.model import Conversation, WorkspaceFile
from pkg.sqlalchemy import SQLAlchemy


@inject
@dataclass
class AgentConversationCleanupService:
    """清理 checkpoint、Workspace 和会话级 Sandbox。"""

    db: SQLAlchemy

    def cleanup_conversation(
        self,
        *,
        account_id: UUID,
        app_id: UUID,
        conversation_id: UUID,
    ) -> None:
        """清理整个会话的派生状态。"""
        self._delete_checkpoint(account_id, app_id, conversation_id)
        now = datetime.now(timezone.utc)
        try:
            self.db.session.query(WorkspaceFile).filter(
                WorkspaceFile.account_id == account_id,
                WorkspaceFile.app_id == app_id,
                WorkspaceFile.conversation_id == conversation_id,
            ).update(
                {
                    WorkspaceFile.is_deleted: True,
                    WorkspaceFile.updated_at: now,
                },
                synchronize_session=False,
            )
            self.db.session.commit()
            self._delete_sandbox(
                account_id=account_id,
                app_id=app_id,
                conversation_id=conversation_id,
            )
        except Exception:
            self.db.session.rollback()
            raise

    def cleanup_message(
        self,
        *,
        account_id: UUID,
        app_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
    ) -> None:
        """清理单条消息关联的可恢复状态。"""
        self._delete_checkpoint(account_id, app_id, conversation_id)

    def cleanup_app(self, *, account_id: UUID, app_id: UUID) -> None:
        """清理应用下全部 Agent 派生状态。"""
        conversation_ids = [
            row[0]
            for row in self.db.session.query(Conversation.id)
            .filter(Conversation.app_id == app_id)
            .all()
        ]
        for conversation_id in conversation_ids:
            self._delete_checkpoint(account_id, app_id, conversation_id)

        now = datetime.now(timezone.utc)
        try:
            self.db.session.query(WorkspaceFile).filter(
                WorkspaceFile.account_id == account_id,
                WorkspaceFile.app_id == app_id,
            ).update(
                {
                    WorkspaceFile.is_deleted: True,
                    WorkspaceFile.updated_at: now,
                },
                synchronize_session=False,
            )
            self.db.session.commit()
            for conversation_id in conversation_ids:
                self._delete_sandbox(
                    account_id=account_id,
                    app_id=app_id,
                    conversation_id=conversation_id,
                )
        except Exception:
            self.db.session.rollback()
            raise

    @staticmethod
    def _delete_checkpoint(
        account_id: UUID,
        app_id: UUID,
        conversation_id: UUID,
    ) -> None:
        """删除会话对应的 LangGraph 线程。"""
        checkpointer = get_configured_checkpointer()
        if checkpointer is False:
            return
        delete_thread = getattr(checkpointer, "delete_thread", None)
        if not callable(delete_thread):
            # InMemorySaver is process-scoped and intentionally has no
            # per-thread deletion API. Its state disappears with the process.
            return
        delete_thread(
            build_thread_id(
                account_id=str(account_id),
                app_id=str(app_id),
                conversation_id=str(conversation_id),
            )
        )

    @staticmethod
    def _delete_sandbox(
        *,
        account_id: UUID,
        app_id: UUID,
        conversation_id: UUID,
    ) -> None:
        """删除会话级 Sandbox；普通请求结束不走这条路径。"""
        if Config().AGENT_SANDBOX_PROVIDER != "langsmith":
            return

        from langsmith.sandbox import ResourceNotFoundError, SandboxClient

        name = build_stable_sandbox_name(
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation_id,
            agent_id="main",
        )
        try:
            SandboxClient().delete_sandbox(name)
        except ResourceNotFoundError:
            return
        except Exception:
            # Remote cleanup is best-effort; the sandbox idle TTL remains the
            # fallback when the provider is temporarily unavailable.
            return


__all__ = ["AgentConversationCleanupService"]
