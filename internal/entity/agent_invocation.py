"""一次执行的可信输入；JSON 字段保持与历史恢复快照兼容。"""

from __future__ import annotations

from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from internal.entity.agent_attachment_entity import AgentAttachment
from internal.entity.agent_run_entity import AgentModelSelection
from internal.entity.conversation_entity import InvokeFrom


class AgentInvocation(BaseModel):
    """创建 Run 时保存的完整执行输入，后台装配和恢复不依赖 HTTP 请求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: UUID
    app_id: UUID
    conversation_id: UUID
    created_by: UUID
    end_user_id: UUID | None
    invoke_from: InvokeFrom

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
    model_selection: AgentModelSelection | None = None
    # 旧客户端会自动回传默认模型，只有新运行显式标记后才恢复会话覆盖。
    model_selection_explicit: bool = False
