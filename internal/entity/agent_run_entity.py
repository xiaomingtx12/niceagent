"""Agent Run 领域实体，不包含 Flask 或数据库查询细节。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictStr

_InvocationReqT = TypeVar("_InvocationReqT")


class AgentRunStatus(str, Enum):
    """Agent Run 生命周期状态；取消请求由 cancel_requested_at 表达，不占用状态位。"""

    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 正在执行、持有执行租约的唯一状态。
AGENT_RUN_ACTIVE_STATUSES = (AgentRunStatus.RUNNING.value,)
# 仍占用会话的状态；等待输入虽无执行进程，但不允许同会话启动新任务。
AGENT_RUN_OCCUPYING_STATUSES = (
    AgentRunStatus.RUNNING.value,
    AgentRunStatus.WAITING_INPUT.value,
)
AGENT_RUN_TERMINAL_STATUSES = frozenset(
    {
        AgentRunStatus.COMPLETED.value,
        AgentRunStatus.FAILED.value,
        AgentRunStatus.CANCELLED.value,
    }
)


class AgentModelSelection(BaseModel):
    """仅允许引用模型，不接受客户端传入凭证、地址或任意参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    provider: StrictStr = Field(min_length=1, max_length=128)
    model: StrictStr = Field(min_length=1, max_length=255)
    provider_connection_id: UUID


class AgentRunInput(BaseModel):
    """从 Agent Protocol 命令中提取出的可信运行输入。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: StrictStr = Field(min_length=1)
    planning_enabled: bool = Field(default=False, strict=True)
    image_urls: tuple[StrictStr, ...] = Field(default_factory=tuple, max_length=10)
    file_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=5)
    selected_skills: tuple[StrictStr, ...] = Field(default_factory=tuple, max_length=20)
    input_message_id: StrictStr | None = None
    reasoning_effort: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    model_selection: AgentModelSelection | None = None


@dataclass
class AgentRunExecution(Generic[_InvocationReqT]):
    """当前进程持有的执行片段；Run 身份稳定，每次恢复分配新的 executor_id。"""

    run_id: UUID
    conversation_id: UUID
    spec: _InvocationReqT
    executor_id: UUID = field(default_factory=uuid4)
    initial_event_sequence: int = 0
    last_event_sequence: int = 0
    done: threading.Event = field(default_factory=threading.Event)
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    resume: dict[str, Any] | None = None


__all__ = [
    "AGENT_RUN_ACTIVE_STATUSES",
    "AGENT_RUN_OCCUPYING_STATUSES",
    "AGENT_RUN_TERMINAL_STATUSES",
    "AgentRunExecution",
    "AgentRunInput",
    "AgentRunStatus",
]
