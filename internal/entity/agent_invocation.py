"""持久化运行选项与由问答记录组装的可信执行输入。"""

from __future__ import annotations

from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from internal.entity.agent_attachment_entity import AgentAttachment
from internal.entity.agent_run_entity import AgentModelSelection
from internal.entity.conversation_entity import InvokeFrom


class AgentExecutionOptions(BaseModel):
    """只保存恢复所需选项，身份、问题和附件由关联记录及当前授权取得。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    planning_enabled: bool = Field(default=False, strict=True)
    selected_skills: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    user_input_enabled: bool = False
    definition_fingerprint: str | None = None
    reasoning_effort: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    model_selection: AgentModelSelection | None = None
    # 旧客户端会自动回传默认模型，只有新运行显式标记后才恢复会话覆盖。
    model_selection_explicit: bool = False


class AgentInvocation(AgentExecutionOptions):
    """本次执行的完整可信输入；内存中携带身份与内容，数据库仅保存选项。"""

    account_id: UUID
    app_id: UUID
    conversation_id: UUID
    created_by: UUID
    end_user_id: UUID | None
    invoke_from: InvokeFrom
    turn_id: UUID
    query: str
    image_urls: tuple[str, ...] = Field(default_factory=tuple)
    attachments: tuple[AgentAttachment, ...] = Field(default_factory=tuple)
    input_message_id: str | None = None

    def execution_options(self) -> dict:
        """以选项 DTO 的字段集合保存，避免新增调用上下文时意外重复持久化。"""
        return self.model_dump(mode="json", include=set(AgentExecutionOptions.model_fields))
