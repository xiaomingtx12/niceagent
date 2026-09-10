"""Agent 结构化遥测接口。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping

from internal.core.agent_v2.entity.telemetry import (
    AgentTelemetryEvent,
    TelemetryValue,
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
        """绑定输出端和脱敏基础属性。"""
        self._record = record
        self._attributes = self._sanitize(attributes or {})

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
        merged = {**self._attributes, **self._sanitize(attributes or {})}
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

    @staticmethod
    def _sanitize(
        attributes: Mapping[str, TelemetryValue | None],
    ) -> dict[str, TelemetryValue]:
        """保留适合指标系统的非空标量。"""
        return {
            str(key): value
            for key, value in attributes.items()
            if value is not None and isinstance(value, (str, int, float, bool))
        }


def build_path_digest(path: str) -> str:
    """生成不暴露原始路径的稳定遥测标识。"""
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


NOOP_AGENT_TELEMETRY = AgentTelemetry()

__all__ = [
    "AgentTelemetry",
    "build_path_digest",
    "NOOP_AGENT_TELEMETRY",
]
