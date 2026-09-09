"""Agent 单次调用和派生资源清理请求 Schema。"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from internal.entity.agent_attachment_entity import AgentAttachment
from internal.entity.conversation_entity import InvokeFrom
from internal.entity.agent_workbench_entity import WorkbenchModelSelection


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
    planning_enabled: bool = Field(default=False, strict=True)
    image_urls: tuple[str, ...] = Field(default_factory=tuple)
    attachments: tuple[AgentAttachment, ...] = Field(default_factory=tuple)
    selected_skills: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    input_message_id: str | None = None
    user_input_enabled: bool = False
    definition_fingerprint: str | None = None
    reasoning_effort: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    model_selection: WorkbenchModelSelection | None = None


class WorkbenchModelChoiceResp(BaseModel):
    """工作台模型目录仅公开标识、名称和推理等级。"""

    provider: str
    model: str
    provider_connection_id: str
    label: str
    connection_name: str
    reasoning_levels: list[str] = Field(default_factory=list)
    default_reasoning_effort: str | None = None


class WorkbenchModelSettingsResp(BaseModel):
    """只有创建者入口公开可切换的模型目录。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    model: str = ""
    provider: str = ""
    selected_model: WorkbenchModelSelection | None = None
    available_models: list[WorkbenchModelChoiceResp] = Field(default_factory=list)
    reasoning_levels: list[str] = Field(default_factory=list)
    default_reasoning_effort: str | None = None
    selected_reasoning_effort: str | None = None


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
