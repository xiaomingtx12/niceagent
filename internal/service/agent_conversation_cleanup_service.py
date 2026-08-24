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
        # 1.删除 LangGraph checkpoint，阻止会话继续恢复旧状态。
        self._delete_checkpoint(account_id, app_id, conversation_id)
        now = datetime.now(timezone.utc)
        try:
            # 2.软删除会话下的 Workspace 文件版本。
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
            # 3.删除会话级 Sandbox，释放远程运行资源。
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
        # 1.消息删除后清理对应会话 checkpoint，避免恢复到已删除输入。
        self._delete_checkpoint(account_id, app_id, conversation_id)

    def cleanup_app(self, *, account_id: UUID, app_id: UUID) -> None:
        """清理应用下全部 Agent 派生状态。"""
        # 1.查询应用下的会话，逐个删除线程 checkpoint。
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
            # 2.批量软删除应用下的 Workspace 文件。
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
            # 3.逐个删除应用下的会话 Sandbox。
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
        # 1.获取平台配置的 Checkpointer，未配置时无需清理。
        checkpointer = get_configured_checkpointer()
        if checkpointer is False:
            return
        delete_thread = getattr(checkpointer, "delete_thread", None)
        if not callable(delete_thread):
            # InMemorySaver 只在进程内保存状态，没有按线程删除接口；
            # 进程结束后状态会自然消失。
            return
        # 2.使用与运行时相同的 thread_id 删除持久化状态。
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
        # 1.只有远程 Sandbox provider 才需要调用删除 API。
        if Config().AGENT_SANDBOX_PROVIDER != "langsmith":
            return

        from langsmith.sandbox import ResourceNotFoundError, SandboxClient

        # 2.按租户、应用和会话生成稳定的 Sandbox 名称。
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
            # 3.远程清理失败时保留 TTL 作为兜底，不影响业务删除。
            # 远程清理采用尽力而为策略，provider 暂时不可用时由 Sandbox 空闲 TTL 兜底。
            return


__all__ = ["AgentConversationCleanupService"]
