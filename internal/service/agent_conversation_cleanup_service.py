"""清理 Agent 会话派生状态。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from injector import inject

from config.config import Config
from internal.core.agent_v2.backends.sandbox.sandbox_backend_factory import (
    build_stable_sandbox_name,
)
from internal.core.agent_v2.persistence.langgraph_checkpoint_provider import (
    get_configured_checkpointer,
)
from internal.core.agent_v2.persistence.agent_memory_repository import (
    AgentMemoryRepository,
)
from internal.extension.logging_extension import logger
from internal.model import App, Conversation
from internal.repository import WorkspaceFileStore
from internal.schema.agent_runtime_schema import (
    AgentAppCleanupReq,
    AgentConversationCleanupReq,
    AgentMessageCleanupReq,
)
from pkg.sqlalchemy import SQLAlchemy

@inject
@dataclass
class AgentConversationCleanupService:
    """清理 checkpoint、Workspace 和会话级 Sandbox。"""

    db: SQLAlchemy
    workspace_file_store: WorkspaceFileStore
    agent_memory_repository: AgentMemoryRepository

    def cleanup_conversation(
        self,
        req: AgentConversationCleanupReq,
    ) -> None:
        """清理整个会话的派生状态。"""
        # 1.删除 LangGraph checkpoint，阻止会话继续恢复旧状态。
        self._delete_checkpoint(req.conversation_id)
        # Workspace 按账号、Agent 和用户共享，删除一段会话不能删除共享文件。
        # 2.数据库状态处理后再尽力释放当前会话的远程运行资源。
        self._delete_sandbox(
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
        self._delete_checkpoint(req.conversation_id)

    def cleanup_app(self, req: AgentAppCleanupReq) -> None:
        """清理应用下全部 Agent 派生状态。"""
        # 1.查询应用下的会话，逐个删除 checkpoint。
        conversation_ids = [
            row[0]
            for row in self.db.session.query(Conversation.id)
            .join(App, App.id == Conversation.app_id)
            .filter(
                Conversation.app_id == req.app_id,
                App.account_id == req.account_id,
            )
            .all()
        ]
        for conversation_id in conversation_ids:
            self._delete_checkpoint(conversation_id)

        # 2.批量软删除应用下全部用户 Workspace 文件并提交事务。
        self.workspace_file_store.delete_app_files(
            self.db.session,
            account_id=req.account_id,
            app_id=req.app_id,
        )
        # 3.逐个尽力删除应用下的会话 Sandbox。
        for conversation_id in conversation_ids:
            self._delete_sandbox(
                account_id=req.account_id,
                app_id=req.app_id,
                conversation_id=conversation_id,
            )
        # 4.应用是长期记忆作用域的一部分，删除应用时清理所有主体记忆。
        self.agent_memory_repository.delete_app_memories(
            account_id=str(req.account_id),
            app_id=str(req.app_id),
        )

    @staticmethod
    def _delete_checkpoint(conversation_id: UUID) -> None:
        """删除会话对应的 LangGraph checkpoint。"""
        # 1.获取平台配置的 Checkpointer，未配置时无需清理。
        checkpointer = get_configured_checkpointer()
        if checkpointer is False:
            return
        delete_thread = getattr(checkpointer, "delete_thread", None)
        if not callable(delete_thread):
            # InMemorySaver 只在进程内保存状态，没有按线程删除接口；
            # 进程结束后状态会自然消失。
            return
        # 2.LangGraph 的固定字段直接保存业务会话 ID。
        delete_thread(str(conversation_id))

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
            # 远程资源有空闲 TTL 兜底，删除失败不回滚已提交的业务删除。
            logger.warning(
                "删除 Agent Sandbox 失败，将等待 TTL 回收: "
                "account_id=%s app_id=%s conversation_id=%s sandbox=%s",
                account_id,
                app_id,
                conversation_id,
                name,
                exc_info=True,
            )
            return


__all__ = ["AgentConversationCleanupService"]
