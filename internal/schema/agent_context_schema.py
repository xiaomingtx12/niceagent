"""上下文窗口公开事件的固定结构，禁止透传内部请求正文。"""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class ContextSectionResp(BaseModel):
    """窗口组成只含类别计数，内容与参数不能进入公开载荷。"""

    model_config = ConfigDict(extra="ignore")
    key: Literal["system", "memory", "human", "assistant", "tool", "tools"]
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
    sections: list[ContextSectionResp] = Field(max_length=6)
    has_multimodal: bool = False
