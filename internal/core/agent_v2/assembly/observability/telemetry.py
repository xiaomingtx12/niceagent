"""Agent V2 的结构化遥测边界。"""

from __future__ import annotations

import hashlib
from typing import Mapping, Protocol

from internal.core.agent_v2.entity.telemetry import (
    AgentTelemetryEvent,
    TelemetryValue,
)
from internal.extension.logging_extension import logger


def build_path_digest(path: str) -> str:
    """生成不暴露原始路径的稳定遥测标识。"""
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


class AgentTelemetrySink(Protocol):
    """结构化 Agent 遥测输出协议。"""

    def record(self, event: AgentTelemetryEvent) -> None:
        """保存或导出遥测事件。"""
        ...


class NullAgentTelemetrySink:
    """未配置输出端时丢弃遥测。"""

    def record(self, event: AgentTelemetryEvent) -> None:
        """接收事件但不执行 I/O。"""


class AgentTelemetry:
    """为同一输出端的事件附加稳定运行时作用域。"""

    def __init__(
        self,
        sink: AgentTelemetrySink | None = None,
        *,
        attributes: Mapping[str, TelemetryValue | None] | None = None,
    ) -> None:
        """绑定输出端和脱敏基础属性。"""
        self._sink = sink or NullAgentTelemetrySink()
        self._attributes = self._sanitize(attributes or {})

    def emit(
        self,
        name: str,
        *,
        value: float = 1.0,
        attributes: Mapping[str, TelemetryValue | None] | None = None,
    ) -> None:
        """隔离观察端故障并发送事件。"""
        merged = {**self._attributes, **self._sanitize(attributes or {})}
        try:
            self._sink.record(
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


NOOP_AGENT_TELEMETRY = AgentTelemetry()

__all__ = [
    "AgentTelemetry",
    "AgentTelemetrySink",
    "build_path_digest",
    "NOOP_AGENT_TELEMETRY",
    "NullAgentTelemetrySink",
]
