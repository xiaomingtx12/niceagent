"""内部 Agent Protocol 的标准 SDK 边界 Schema。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator


class InternalThreadResp(BaseModel):
    """将平台会话投影为 SDK 固定的 thread 状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: UUID
    created_at: StrictStr
    updated_at: StrictStr
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: Literal["idle", "busy"]
    values: dict[str, Any] = Field(default_factory=dict)
    interrupts: dict[str, Any] = Field(default_factory=dict)


class InternalRunCreateReq(BaseModel):
    """LangGraph SDK runs.create 的最小请求。"""

    model_config = ConfigDict(extra="ignore")

    assistant_id: StrictStr
    input: dict[str, Any] | None = None
    command: dict[str, Any] | None = None
    multitask_strategy: Literal[
        "reject", "interrupt", "rollback", "enqueue"
    ] | None = None

    @model_validator(mode="after")
    def validate_execution_payload(self) -> "InternalRunCreateReq":
        """确保 SDK 一次 Run 只携带一种执行载荷。"""
        if (self.input is None) == (self.command is None):
            raise ValueError("Run 必须且只能提供 input 或 command")
        return self


class InternalRunResp(BaseModel):
    """SDK 可消费的后台 Run 摘要；thread_id 的值就是会话 ID。"""

    run_id: UUID
    thread_id: UUID
    assistant_id: StrictStr
    status: Literal["pending", "running", "success", "error", "cancelled"]
    multitask_strategy: Literal[
        "reject", "interrupt", "rollback", "enqueue"
    ] = "reject"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: StrictStr
    updated_at: StrictStr
    error: StrictStr | None = None


@dataclass(frozen=True)
class InternalRunLaunchResp:
    """内部 SDK 创建 Run 的业务结果。"""

    run: InternalRunResp | None = None
    conflict_message: str | None = None


__all__ = [
    "InternalRunCreateReq",
    "InternalRunLaunchResp",
    "InternalRunResp",
    "InternalThreadResp",
]
