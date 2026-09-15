"""内部 Agent Protocol 的标准 SDK 边界 Schema。"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator
from internal.entity import AgentExecutionInput
from internal.schema.agent.agent_protocol_schema import AgentProtocolInputOptionsReq, AgentProtocolUserMessageReq


class InternalRunInputReq(AgentProtocolInputOptionsReq):
    """内部 SDK 的图输入，独立于公开 commands 的消息 ID 要求。"""

    model_config = ConfigDict(extra="ignore")

    messages: list[AgentProtocolUserMessageReq] = Field(default_factory=list)
    query: StrictStr | None = None

    def to_run_input(self) -> AgentExecutionInput:
        """把 SDK 消息或 query 转成业务输入，不替调用者伪造幂等标识。"""
        message = self.messages[-1] if self.messages else None
        return self.to_execution_input(
            query=message.text if message is not None else (self.query or ""),
            message_id=message.id if message is not None else None,
        )


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
    multitask_strategy: Literal["reject", "interrupt"] | None = None

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
    multitask_strategy: Literal["reject", "interrupt"] = "reject"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: StrictStr
    updated_at: StrictStr
    error: StrictStr | None = None


__all__ = [
    "InternalRunInputReq",
    "InternalRunCreateReq",
    "InternalRunResp",
    "InternalThreadResp",
]
