"""工作台模型调用边界，不包含提示词、参数或供应商错误正文。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentModelProgressResp(BaseModel):
    """调用 ID 与消息流共用，用于合并请求等待和内容生成阶段。"""

    model_config = ConfigDict(extra="ignore")
    type: Literal["model_progress"]
    model_run_id: str = Field(min_length=1, max_length=64)
    phase: Literal["started", "completed", "failed", "cancelled"]
    started_at: int = Field(gt=0)
    ended_at: int | None = Field(default=None, gt=0)
    duration_ms: int = Field(ge=0)
