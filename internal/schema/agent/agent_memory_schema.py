"""工作台记忆提交事件，身份沿用外层协议的事件标识。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from internal.core.agent_v2.entity.agent_memory import (
    normalize_agent_memory_path,
)


class AgentMemoryUpdateResp(BaseModel):
    """与实时输出、历史重放共用同一公开格式。"""

    model_config = ConfigDict(extra="ignore")
    type: Literal["memory_file_update"]
    path: str = Field(min_length=1, max_length=512)
    action: Literal["write", "delete"]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """公开事件仅允许当前记忆作用域的文件路径。"""
        return normalize_agent_memory_path(value)
