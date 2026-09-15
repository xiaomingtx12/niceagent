"""上下文窗口公开事件的固定结构，禁止透传内部请求正文。"""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class ContextSectionResp(BaseModel):
    """窗口组成只含类别计数，内容与参数不能进入公开载荷。"""

    model_config = ConfigDict(extra="ignore")
    key: Literal["system", "memory", "human", "assistant", "tool", "tools", "summary"]
    label: str = Field(max_length=40)
    tokens: int = Field(ge=0)
    count: int = Field(ge=0)


class ContextWindowResp(BaseModel):
    """区分完整窗口和输入上限，以及实际计数与估算。"""

    model_config = ConfigDict(extra="ignore")
    type: Literal["context_window"]
    call_id: str = Field(max_length=64)
    status: Literal["running", "completed", "failed"]
    model: str = Field(max_length=256)
    context_limit: int | None = Field(default=None, gt=0)
    limit_kind: Literal["context", "input"]
    output_reserve: int | None = Field(default=None, gt=0)
    estimated_input_tokens: int = Field(ge=0)
    actual_input_tokens: int | None = Field(default=None, ge=0)
    max_input_tokens: int | None = Field(default=None, gt=0)
    compaction_trigger_tokens: int | None = Field(default=None, gt=0)
    compaction_input_tokens: int | None = Field(default=None, ge=0)
    sections: list[ContextSectionResp] = Field(max_length=7)
    has_multimodal: bool = False
    input_budget: int | None = Field(default=None, gt=0)
    safety_tokens: int | None = Field(default=None, gt=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_creation_tokens: int | None = Field(default=None, ge=0)
    cache_read_ratio: float | None = Field(default=None, ge=0, le=1)
    duration_ms: int | None = Field(default=None, ge=0)
    system_fingerprint: str | None = Field(default=None, max_length=64)
    tools_fingerprint: str | None = Field(default=None, max_length=64)


class ContextCompactionResp(BaseModel):
    """区分摘要调用观测与旧版压缩记录；未知压缩指标保持空值。"""

    model_config = ConfigDict(extra="ignore")
    type: Literal["context_compaction"]
    compaction_id: str = Field(min_length=1, max_length=64)
    phase: Literal["preparing", "summarizing", "completed", "failed", "cancelled"]
    scope: Literal["compression", "summary_generation", "checkpoint"] = "compression"
    reason: Literal["threshold", "overflow", "manual", "automatic"] | None = None
    summary_id: str | None = Field(default=None, max_length=64)
    first_token_ms: int | None = Field(default=None, ge=0)
    started_at: int | None = Field(default=None, gt=0)
    ended_at: int | None = Field(default=None, gt=0)
    duration_ms: int | None = Field(default=None, ge=0)
    before_tokens: int | None = Field(default=None, ge=0)
    after_tokens: int | None = Field(default=None, ge=0)
    summarized_messages: int | None = Field(default=None, ge=0)
    preserved_messages: int | None = Field(default=None, ge=0)
    history_saved: bool | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class ConversationSummaryResp(BaseModel):
    """按需返回当前有效摘要，空会话或未压缩时 summary_id/content 均为空。"""

    model_config = ConfigDict(extra="ignore")
    summary_id: str | None = None
    content: str | None = None
    created_at: str | None = None
    status: Literal["persisted", "none"] = "none"
    history_saved: bool | None = None
    reason: Literal["manual", "automatic"] | None = None
