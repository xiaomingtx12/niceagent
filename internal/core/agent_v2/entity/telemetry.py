"""Agent 运行遥测的领域事件和值类型。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Mapping, TypeAlias

TelemetryValue: TypeAlias = str | int | float | bool


def normalize_attributes(
    values: Mapping[str, Any] | None, *, allowed_keys: tuple[str, ...] | None = None,
) -> dict[str, TelemetryValue]:
    """统一标量过滤与长度限制；业务敏感字段仍由调用方白名单约束。"""
    if not isinstance(values, Mapping):
        return {}
    keys = allowed_keys if allowed_keys is not None else values.keys()
    attributes: dict[str, TelemetryValue] = {}
    for key in keys:
        value = values.get(key)
        if isinstance(value, bool):
            attributes[str(key)] = value
        elif isinstance(value, (int, float)) and isfinite(value):
            attributes[str(key)] = value
        elif isinstance(value, str) and value:
            attributes[str(key)] = value[:256]
    return attributes


@dataclass(frozen=True)
class AgentTelemetryEvent:
    """Agent 运行产生的结构化指标事件。"""

    name: str
    value: float = 1.0
    attributes: Mapping[str, TelemetryValue] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


__all__ = ["AgentTelemetryEvent", "TelemetryValue", "normalize_attributes"]
