"""Agent 运行时装配和清理请求 Schema。"""

from __future__ import annotations

from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

from internal.core.agent_v2.entity.subagent_config import SubAgentConfig
from internal.entity.agent_attachment_entity import AgentAttachment
from internal.entity.conversation_entity import InvokeFrom
from internal.schema.app_config_schema import AgentAppConfigReq
from internal.schema.skill_schema import SkillBindingReq


class AgentInvocation(BaseModel):
    """Service 交给 Agent 装配层的一次可信调用参数。"""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        frozen=True,
    )

    account_id: UUID
    app_id: UUID
    app_name: str
    app_description: str
    app_config: AgentAppConfigReq
    conversation_id: UUID
    message_id: UUID
    created_by: UUID
    user_id: UUID
    end_user_id: UUID | None
    invoke_from: InvokeFrom
    query: str
    image_urls: tuple[str, ...] = Field(default_factory=tuple)
    attachments: tuple[AgentAttachment, ...] = Field(default_factory=tuple)
    opening_statement: str = ""
    extra_tools: tuple[BaseTool, ...] = Field(default_factory=tuple)
    llm: BaseChatModel | None = None
    input_message_id: str | None = None


class AgentRuntimePrepareReq(BaseModel):
    """装配一次隔离 Agent 运行时所需的请求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: UUID
    app_id: UUID
    conversation_id: UUID
    created_by: UUID
    principal_id: UUID
    memory_enabled: bool = Field(default=False, strict=True)
    skill_bindings: tuple[SkillBindingReq, ...] = Field(default_factory=tuple)
    subagents: tuple[SubAgentConfig, ...] = Field(default_factory=tuple)


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
    "AgentMessageCleanupReq",
    "AgentRuntimePrepareReq",
]
