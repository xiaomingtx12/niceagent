"""Agent 单次调用和派生资源清理请求 Schema。"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from internal.entity.agent_attachment_entity import AgentAttachment
from internal.entity.conversation_entity import InvokeFrom


class AgentInvocationContext(BaseModel):
    """入口完成鉴权后交给运行模块的可信调用上下文。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: UUID
    app_id: UUID
    conversation_id: UUID
    created_by: UUID
    end_user_id: UUID | None
    invoke_from: InvokeFrom


class AgentInvocation(AgentInvocationContext):
    """Service 交给 Agent 装配层的一次可信调用参数。"""

    message_id: UUID
    query: str
    image_urls: tuple[str, ...] = Field(default_factory=tuple)
    attachments: tuple[AgentAttachment, ...] = Field(default_factory=tuple)
    selected_skills: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    input_message_id: str | None = None


class AgentConversationCleanupReq(BaseModel):
    """清理会话派生资源的请求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: UUID
    app_id: UUID
    conversation_id: UUID


class AgentMessageCleanupReq(AgentConversationCleanupReq):
    """清理消息所在会话可恢复状态的请求。"""

    message_id: UUID


class AgentAppCleanupReq(BaseModel):
    """清理应用下全部 Agent 派生资源的请求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: UUID
    app_id: UUID


__all__ = [
    "AgentAppCleanupReq",
    "AgentConversationCleanupReq",
    "AgentInvocation",
    "AgentInvocationContext",
    "AgentMessageCleanupReq",
]
