"""Agent 运行遥测的领域事件和值类型。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, TypeAlias

TelemetryValue: TypeAlias = str | int | float | bool


@dataclass(frozen=True)
class AgentTelemetryEvent:
    """Agent 运行产生的结构化指标事件。"""

    name: str
    value: float = 1.0
    attributes: Mapping[str, TelemetryValue] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


__all__ = ["AgentTelemetryEvent", "TelemetryValue"]
