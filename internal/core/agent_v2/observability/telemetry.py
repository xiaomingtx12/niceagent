"""Agent 结构化遥测接口。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping

from internal.core.agent_v2.entity.telemetry import (
    AgentTelemetryEvent,
    TelemetryValue,
    normalize_attributes,
)
from internal.extension.logging_extension import logger


class AgentTelemetry:
    """为同一输出端的事件附加稳定运行时作用域。"""

    def __init__(
        self,
        record: Callable[[AgentTelemetryEvent], None] | None = None,
        *,
        attributes: Mapping[str, TelemetryValue | None] | None = None,
    ) -> None:
        """固定执行作用域，单条事件只能补充属性。"""
        self._record = record
        self._attributes = normalize_attributes(attributes)

    def emit(
        self,
        name: str,
        *,
        value: float = 1.0,
        attributes: Mapping[str, TelemetryValue | None] | None = None,
    ) -> None:
        """隔离观察端故障并发送事件。"""
        if self._record is None:
            return
        merged = {**normalize_attributes(attributes), **self._attributes}
        try:
            self._record(
                AgentTelemetryEvent(
                    name=name,
                    value=value,
                    attributes=merged,
                )
            )
        except Exception:
            logger.exception("Agent telemetry sink failed for %s", name)


def build_path_digest(path: str) -> str:
    """生成不暴露原始路径的稳定遥测标识。"""
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


NOOP_AGENT_TELEMETRY = AgentTelemetry()

__all__ = [
    "AgentTelemetry",
    "build_path_digest",
    "NOOP_AGENT_TELEMETRY",
]
