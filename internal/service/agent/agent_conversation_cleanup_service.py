"""清理 Agent 会话派生状态。"""

from __future__ import annotations

from dataclasses import dataclass

from injector import inject

from config.config import Config
from internal.core.agent_v2.persistence.langgraph_checkpoint_provider import (
    delete_agent_checkpoint,
)
from internal.core.agent_v2.backends.sandbox.sandbox_backend_factory import (
    delete_agent_sandbox,
)
from internal.core.agent_v2.persistence.agent_memory_repository import (
    AgentMemoryRepository,
)
from internal.extension.logging_extension import logger
from internal.model import App, Conversation
from internal.repository import WorkspaceFileStore
from internal.schema.agent.agent_runtime_schema import (
    AgentAppCleanupReq,
    AgentConversationCleanupReq,
    AgentMessageCleanupReq,
)
from pkg.sqlalchemy import SQLAlchemy


@inject
@dataclass
class AgentConversationCleanupService:
    """决定消息、会话和应用删除时的清理范围，底层资源由 Agent Core 清理。"""

    db: SQLAlchemy
    workspace_file_store: WorkspaceFileStore
    agent_memory_repository: AgentMemoryRepository
    config: Config

    def cleanup_conversation(
        self,
        req: AgentConversationCleanupReq,
    ) -> None:
        """清理整个会话的派生状态。"""
        # 1.删除 LangGraph checkpoint，阻止会话继续恢复旧状态。
        delete_agent_checkpoint(str(req.conversation_id))
        # Workspace 按账号、Agent 和用户共享，删除一段会话不能删除共享文件。
        # 2.数据库状态处理后再尽力释放当前会话的远程运行资源。
        delete_agent_sandbox(
            settings=self.config,
            account_id=req.account_id,
            app_id=req.app_id,
            conversation_id=req.conversation_id,
        )

    def cleanup_message(
        self,
        req: AgentMessageCleanupReq,
    ) -> None:
        """清理单条消息所在会话的可恢复状态。"""
        # 1.消息删除后清理对应会话 checkpoint，避免恢复到已删除输入。
        logger.debug("清理消息关联的 Agent checkpoint: message_id=%s", req.message_id)
        delete_agent_checkpoint(str(req.conversation_id))

    def cleanup_app(self, req: AgentAppCleanupReq) -> None:
        """清理应用下全部 Agent 派生状态。"""
        # 1.查询应用下的会话，逐个删除 checkpoint。
        conversation_rows = (
            self.db.session.query(Conversation.id)
            .join(App, App.id == Conversation.app_id)
            .filter(
                Conversation.app_id == req.app_id,
                App.account_id == req.account_id,
            )
            .all()
        )
        conversation_ids = [row[0] for row in conversation_rows]
        for conversation_id in conversation_ids:
            delete_agent_checkpoint(str(conversation_id))

        # 2.批量软删除应用下全部用户的共享 Workspace 文件。
        self.workspace_file_store.delete_app_files(
            self.db.session,
            account_id=req.account_id,
            app_id=req.app_id,
        )
        # 3.逐个尽力删除应用下的会话 Sandbox。
        for conversation_id in conversation_ids:
            delete_agent_sandbox(
                settings=self.config,
                account_id=req.account_id,
                app_id=req.app_id,
                conversation_id=conversation_id,
            )
        # 4.应用是长期记忆作用域的一部分，删除应用时清理所有主体记忆。
        self.agent_memory_repository.delete_app_memories(
            account_id=str(req.account_id),
            app_id=str(req.app_id),
        )


__all__ = ["AgentConversationCleanupService"]
