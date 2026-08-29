"""Agent V2 的结构化遥测边界。"""

from __future__ import annotations

import json
import logging
from typing import Mapping, Protocol

from internal.core.agent_v2.entity.telemetry import (
    AgentTelemetryEvent,
    TelemetryValue,
)


class AgentTelemetrySink(Protocol):
    """结构化 Agent 遥测输出协议。"""

    def record(self, event: AgentTelemetryEvent) -> None:
        """保存或导出遥测事件。"""
        ...


class NullAgentTelemetrySink:
    """未配置输出端时丢弃遥测。"""

    def record(self, event: AgentTelemetryEvent) -> None:
        """接收事件但不执行 I/O。"""


class LoggingAgentTelemetrySink:
    """通过应用日志写入结构化遥测。"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        """使用指定日志器或默认遥测日志器。"""
        self._logger = logger or logging.getLogger("my_dify.agent_v2.telemetry")

    def record(self, event: AgentTelemetryEvent) -> None:
        """写入不含模型和文件内容的 JSON 事件。"""
        self._logger.info(
            "agent_runtime_event %s",
            json.dumps(event.to_dict(), ensure_ascii=True, sort_keys=True),
        )


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
            logging.getLogger(__name__).exception(
                "Agent telemetry sink failed for %s", name
            )

    def emit_span(
        self,
        operation: str,
        *,
        phase: str,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None = None,
        status: str | None = None,
        duration_ms: float | None = None,
        attributes: Mapping[str, TelemetryValue | None] | None = None,
    ) -> None:
        """发送带有稳定 Span 关联字段的 Trace 生命周期事件。"""
        span_attributes: dict[str, TelemetryValue | None] = {
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "span_kind": operation,
            "status": status,
            "duration_ms": (
                round(max(0.0, duration_ms), 3) if duration_ms is not None else None
            ),
        }
        span_attributes.update(attributes or {})
        self.emit(
            f"agent.{operation}.{phase}",
            attributes=span_attributes,
        )

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
    "LoggingAgentTelemetrySink",
    "NOOP_AGENT_TELEMETRY",
    "NullAgentTelemetrySink",
]
