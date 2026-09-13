"""工作台领域实体，不包含 Flask 或数据库查询细节。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictStr

_InvocationReqT = TypeVar("_InvocationReqT")


class WorkbenchRunStatus(str, Enum):
    """工作台 Run 生命周期状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    WAITING_INPUT = "waiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


WORKBENCH_ACTIVE_STATUSES = (
    WorkbenchRunStatus.QUEUED.value,
    WorkbenchRunStatus.RUNNING.value,
    WorkbenchRunStatus.CANCELLING.value,
)
WORKBENCH_TERMINAL_STATUSES = frozenset(
    {
        WorkbenchRunStatus.COMPLETED.value,
        WorkbenchRunStatus.FAILED.value,
        WorkbenchRunStatus.CANCELLED.value,
    }
)


class WorkbenchModelSelection(BaseModel):
    """仅允许引用模型，不接受客户端传入凭证、地址或任意参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    provider: StrictStr = Field(min_length=1, max_length=128)
    model: StrictStr = Field(min_length=1, max_length=255)
    provider_connection_id: UUID


class WorkbenchRunInput(BaseModel):
    """从 Agent Protocol 命令中提取出的可信运行输入。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: StrictStr = Field(min_length=1)
    planning_enabled: bool = Field(default=False, strict=True)
    image_urls: tuple[StrictStr, ...] = Field(default_factory=tuple, max_length=10)
    file_ids: tuple[UUID, ...] = Field(default_factory=tuple, max_length=5)
    selected_skills: tuple[StrictStr, ...] = Field(default_factory=tuple, max_length=20)
    input_message_id: StrictStr | None = None
    reasoning_effort: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    model_selection: WorkbenchModelSelection | None = None


@dataclass
class WorkbenchRun(Generic[_InvocationReqT]):
    """当前进程内正在排队或执行的工作台 Run。"""

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
    "WORKBENCH_ACTIVE_STATUSES",
    "WORKBENCH_TERMINAL_STATUSES",
    "WorkbenchRun",
    "WorkbenchRunInput",
    "WorkbenchRunStatus",
]
