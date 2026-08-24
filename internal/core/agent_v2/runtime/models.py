"""Agent V2 的运行时模型和线程边界。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission


def build_memory_namespace(
    *,
    account_id: UUID,
    app_id: UUID,
    end_user_id: UUID | None,
) -> tuple[str, ...]:
    """构造运行时和管理接口共用的长期记忆命名空间。"""
    return (
        "mydify",
        str(account_id),
        str(app_id),
        str(end_user_id or "account"),
    )


@dataclass(frozen=True, slots=True)
class ThreadIdentity:
    """一个 Agent 线程的服务端身份边界。"""

    account_id: str
    app_id: str
    conversation_id: str
    agent_id: str = "main"

    @property
    def thread_id(self) -> str:
        """返回稳定的 LangGraph thread_id。"""
        return (
            f"({self.account_id},{self.app_id},"
            f"{self.conversation_id},{self.agent_id})"
        )

    @property
    def metadata(self) -> dict[str, str]:
        """返回可写入 RunnableConfig 的可信元数据。"""
        return {
            "account_id": self.account_id,
            "app_id": self.app_id,
            "conversation_id": self.conversation_id,
            "agent_id": self.agent_id,
        }

    def to_config(self) -> dict[str, Any]:
        """构造 LangGraph RunnableConfig。"""
        return {
            "configurable": {"thread_id": self.thread_id},
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class AgentRunContext:
    """把请求身份固定为不可变的运行时上下文。"""

    account_id: UUID
    app_id: UUID
    conversation_id: UUID
    created_by: UUID
    end_user_id: UUID | None
    agent_id: str = "main"

    @property
    def thread(self) -> ThreadIdentity:
        """返回当前运行对应的线程身份。"""
        return ThreadIdentity(
            account_id=str(self.account_id),
            app_id=str(self.app_id),
            conversation_id=str(self.conversation_id),
            agent_id=self.agent_id,
        )

    @property
    def memory_namespace(self) -> tuple[str, ...]:
        """返回原生 Store 使用的租户和用户命名空间。"""
        return build_memory_namespace(
            account_id=self.account_id,
            app_id=self.app_id,
            end_user_id=self.end_user_id,
        )

    @property
    def telemetry_attributes(self) -> dict[str, str | None]:
        """返回不包含提示词内容的观测属性。"""
        return {
            "account_id": str(self.account_id),
            "app_id": str(self.app_id),
            "conversation_id": str(self.conversation_id),
            "end_user_id": str(self.end_user_id)
            if self.end_user_id
            else None,
        }


@dataclass
class AgentRuntimeResources:
    """承载一次运行的 DeepAgents 原生资源，不参与图规划或执行。"""

    checkpointer: Any
    backend: BackendProtocol
    permissions: list[FilesystemPermission]
    skill_sources: list[str]
    memory_sources: list[str]
    subagent_skill_sources: dict[str, list[str]]
    telemetry: Any
    runtime_context: AgentRunContext
    store: Any | None = None


__all__ = [
    "AgentRunContext",
    "AgentRuntimeResources",
    "ThreadIdentity",
    "build_memory_namespace",
]
