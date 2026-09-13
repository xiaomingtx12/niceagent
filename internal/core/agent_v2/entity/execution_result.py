"""Agent 单次执行结果，不依赖平台 Run 或消息表。"""

from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, Literal


@dataclass(frozen=True)
class AgentExecutionResult:
    """由执行器交付的结果；调用方负责将其映射为平台状态和存储记录。"""

    status: Literal["completed", "cancelled", "failed", "waiting_input"]
    interrupts: tuple[dict[str, Any], ...] = ()
    answer: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    usage: Mapping[str, Any] = field(default_factory=dict)
    error: Exception | None = None
