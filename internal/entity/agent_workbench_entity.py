"""工作台领域实体，不包含 Flask 或数据库查询细节。"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr

if TYPE_CHECKING:
    from internal.model import App, Conversation, EndUser
    from internal.schema.agent_workbench_schema import WorkbenchEvent

_InvocationReqT = TypeVar("_InvocationReqT")


class WorkbenchRunStatus(str, Enum):
    """工作台 Run 生命周期状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
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


@dataclass(frozen=True)
class WorkbenchRunStartReservation:
    """command 入库期间用于阻止同一会话重复启动的占位记录。"""

    run_id: UUID
    input_message_id: str | None


@dataclass(frozen=True)
class AuthorizedWorkbenchContext:
    """一次 Run 启动已校验、可直接交给 Factory 复用的业务对象。"""

    app: App | None
    conversation: Conversation | None
    end_user: EndUser | None = None


class WorkbenchRunInput(BaseModel):
    """从 Agent Protocol 命令中提取出的可信运行输入。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: StrictStr = Field(min_length=1)
    image_urls: tuple[StrictStr, ...] = Field(default_factory=tuple, max_length=10)
    file_ids: tuple[StrictStr, ...] = Field(default_factory=tuple, max_length=5)
    selected_skills: tuple[StrictStr, ...] = Field(default_factory=tuple, max_length=20)
    input_message_id: StrictStr | None = None


@dataclass
class WorkbenchRun(Generic[_InvocationReqT]):
    """当前进程内正在排队或执行的工作台 Run。"""

    run_id: UUID
    conversation_id: UUID
    spec: _InvocationReqT
    status: WorkbenchRunStatus = WorkbenchRunStatus.QUEUED
    initial_event_sequence: int = 0
    last_event_sequence: int = 0
    state_snapshot: dict[str, Any] | None = None
    error_message: str = ""
    events: deque[WorkbenchEvent] = field(default_factory=lambda: deque(maxlen=2000))
    done: threading.Event = field(default_factory=threading.Event)
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    last_cancel_signal_check: float = 0.0
    last_cancel_status_check: float = 0.0


__all__ = [
    "AuthorizedWorkbenchContext",
    "WORKBENCH_ACTIVE_STATUSES",
    "WORKBENCH_TERMINAL_STATUSES",
    "WorkbenchRun",
    "WorkbenchRunInput",
    "WorkbenchRunStartReservation",
    "WorkbenchRunStatus",
]
