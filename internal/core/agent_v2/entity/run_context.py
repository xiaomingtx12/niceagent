"""Agent Run 的会话身份和长期记忆作用域。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .agent_memory import AgentMemoryIdentity
from .workspace_scope import build_workspace_scope_id


@dataclass(frozen=True, slots=True)
class AgentConversationIdentity:
    """一个 Agent 会话的可信服务端身份。"""

    account_id: str
    app_id: str
    conversation_id: str
    agent_id: str = "main"

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
        """将会话 ID 映射到 LangGraph 固定的 thread_id 配置字段。"""
        return {
            "configurable": {"thread_id": self.conversation_id},
            "metadata": self.metadata,
        }


class AgentRunContext(BaseModel):
    """一次 Agent Run 不可变的业务身份上下文。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: UUID
    app_id: UUID
    conversation_id: UUID
    created_by: UUID
    principal_id: UUID
    memory_enabled: bool = Field(default=False, strict=True)
    planning_enabled: bool = Field(default=False, strict=True)
    user_input_enabled: bool = False
    agent_id: str = Field(default="main", min_length=1, max_length=64)

    @property
    def conversation(self) -> AgentConversationIdentity:
        """返回当前 Run 对应的会话身份。"""
        return AgentConversationIdentity(
            account_id=str(self.account_id),
            app_id=str(self.app_id),
            conversation_id=str(self.conversation_id),
            agent_id=self.agent_id,
        )

    @property
    def memory_namespace(self) -> tuple[str, ...]:
        """返回账号和 Agent 共享的原生 Store 命名空间。"""
        return AgentMemoryIdentity(account_id=self.account_id, app_id=self.app_id).namespace

    @property
    def workspace_scope_id(self) -> UUID:
        """返回跨会话共享 Workspace 的稳定内部作用域 ID。"""
        return build_workspace_scope_id(self.account_id, self.app_id)

    @property
    def telemetry_attributes(self) -> dict[str, str | None]:
        """返回不包含提示词内容的观测属性。"""
        return {
            "account_id": str(self.account_id),
            "app_id": str(self.app_id),
            "conversation_id": str(self.conversation_id),
            "principal_id": str(self.principal_id),
            "memory_enabled": str(self.memory_enabled).lower(),
        }


__all__ = [
    "AgentRunContext",
    "AgentConversationIdentity",
]
