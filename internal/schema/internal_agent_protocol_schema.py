"""内部 Agent Protocol 的标准 SDK 边界 Schema。"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator


class InternalThreadCreateReq(BaseModel):
    """LangGraph SDK threads.create 的最小请求。"""

    model_config = ConfigDict(extra="ignore")

    thread_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class InternalThreadResp(BaseModel):
    """SDK 可消费的线程状态。"""

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
    """SDK 可消费的后台 Run 摘要。"""

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


def new_thread_id(value: UUID | None) -> UUID:
    """保留 SDK 指定的线程标识，否则创建新的 UUID。"""
    return value or uuid4()


__all__ = [
    "InternalRunCreateReq",
    "InternalRunResp",
    "InternalThreadCreateReq",
    "InternalThreadResp",
    "new_thread_id",
]
