"""Agent 执行过程的调用链和用量采集。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from time import monotonic
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import ToolMessage
from langchain_core.outputs import LLMResult

from internal.core.agent_v2.observability.token_usage import (
    normalize_llm_result,
)
from internal.core.agent_v2.entity.telemetry import (
    AgentTelemetryEvent,
    TelemetryValue,
)
from internal.core.agent_v2.entity.token_usage import AgentTokenUsageSummary
from internal.core.agent_v2.observability.callback_metadata import (
    _TOOL_METADATA_KEYS,
    _async_subagent_attributes,
    _bounded_text,
    _exception_status,
    _model_identity,
    _response_model_name,
    _safe_attributes,
    _serialized_name,
)
from internal.core.agent_v2.observability.summary_progress import (
    SummaryProgressPublisher,
)


_SUBAGENT_TOOL_NAMES = frozenset({"task", "start_async_task"})


def _utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(UTC)


@dataclass(slots=True)
class _ObservedSpan:
    """保存一条可观测调用在完成前所需的内部状态。"""

    span_id: str
    parent_span_id: str | None
    category: str
    kind: str
    name: str
    started_at: datetime
    started_monotonic: float
    attributes: dict[str, TelemetryValue] = field(default_factory=dict)
    retry_number: int = 0
    status: str = "running"
    duration_ms: float | None = None
    error_type: str | None = None
    usage: dict[str, Any] | None = None

    def finish(
        self,
        *,
        status: str,
        error_type: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """结束 Span 并冻结可持久化结果。"""
        self.status = status
        self.duration_ms = round(
            max(0.0, (monotonic() - self.started_monotonic) * 1000),
            3,
        )
        self.error_type = error_type
        self.usage = usage

    def to_dict(self) -> dict[str, Any]:
        """转换成不包含业务正文的持久化结构。"""
        value: dict[str, Any] = {
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "duration_ms": self.duration_ms,
            "retry_number": self.retry_number,
            "attributes": dict(self.attributes),
        }
        if self.error_type is not None:
            value["error_type"] = self.error_type
        if self.usage is not None:
            value["usage"] = dict(self.usage)
        return value


class PlatformAgentRunObserver(BaseCallbackHandler):
    """通过 LangChain Callback 汇总平台自己的完整 Run 观测数据。

    Callback 负责模型、工具和同步 SubAgent 的自动传播；本类还作为
    ``AgentTelemetry`` 输出端，接收 Workspace 与 Sandbox 运行事件。
    """

    raise_error = False
    run_inline = True

    def __init__(
        self,
        trace_id: UUID,
        *,
        attributes: Mapping[str, Any] | None = None,
        max_spans: int = 500,
        max_stages: int = 100,
        max_events: int = 200,
    ) -> None:
        """创建一次 Run 独占、线程安全且有容量上限的观察器。"""
        super().__init__()
        self._trace_id = trace_id
        self._attributes = _safe_attributes(attributes)
        self._max_spans = max(1, max_spans)
        self._max_stages = max(1, max_stages)
        self._max_events = max(1, max_events)
        self._lock = RLock()
        self._started_at = _utc_now()
        self._started_monotonic = monotonic()
        self._finished_at: datetime | None = None
        self._duration_ms: float | None = None
        self._status = "running"
        self._error_type: str | None = None
        self._parents: dict[str, str | None] = {}
        self._active_spans: dict[str, _ObservedSpan] = {}
        self._summary_progress = SummaryProgressPublisher()
        self._observable_spans: dict[str, _ObservedSpan] = {}
        self._spans: list[_ObservedSpan] = []
        self._stages: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []
        self._usage = AgentTokenUsageSummary()
        self._model_names: set[str] = set()
        self._counters = {
            "model_call_count": 0,
            "tool_call_count": 0,
            "subagent_call_count": 0,
            "retry_count": 0,
            "error_count": 0,
            "cancelled_call_count": 0,
        }
        self._dropped = {"spans": 0, "stages": 0, "events": 0}

    @property
    def run_id(self) -> UUID:
        """返回需要写入 RunnableConfig 的平台 Run 标识。"""
        return self._trace_id

    @property
    def trace_id(self) -> str:
        """返回对外展示的稳定 Trace 标识。"""
        return str(self._trace_id)

    def record(self, event: AgentTelemetryEvent) -> None:
        """接收 AgentTelemetry 的脱敏运行时事件。"""
        value = {
            "name": _bounded_text(event.name),
            "value": float(event.value),
            "occurred_at": event.occurred_at.isoformat(),
            "attributes": _safe_attributes(event.attributes),
        }
        with self._lock:
            if len(self._events) >= self._max_events:
                self._dropped["events"] += 1
                return
            self._events.append(value)

    def record_stage(
        self,
        name: str,
        duration_ms: float,
        **attributes: Any,
    ) -> None:
        """记录装配、流消费和持久化边界的阶段耗时。"""
        stage = {
            "name": _bounded_text(name),
            "duration_ms": round(max(0.0, float(duration_ms)), 3),
            "occurred_at": _utc_now().isoformat(),
            "attributes": _safe_attributes(attributes),
        }
        with self._lock:
            if len(self._stages) >= self._max_stages:
                self._dropped["stages"] += 1
                return
            self._stages.append(stage)

    def finish(
        self,
        status: str,
        *,
        error: BaseException | None = None,
    ) -> None:
        """收敛 Run 终态，并关闭仍未结束的调用 Span。"""
        final_status = (
            status
            if status in {"completed", "failed", "cancelled", "waiting_input"}
            else "failed"
        )
        with self._lock:
            if self._finished_at is not None:
                return
            self._finished_at = _utc_now()
            self._duration_ms = round(
                max(0.0, (monotonic() - self._started_monotonic) * 1000),
                3,
            )
            self._status = final_status
            self._error_type = type(error).__name__ if error is not None else None
            unfinished_status = (
                "cancelled"
                if final_status == "cancelled"
                else "error" if final_status == "failed" else "ok"
            )
            for span in self._active_spans.values():
                span.finish(
                    status=unfinished_status,
                    error_type=(
                        self._error_type if unfinished_status == "error" else None
                    ),
                )
                if unfinished_status == "error":
                    self._counters["error_count"] += 1
                elif unfinished_status == "cancelled":
                    self._counters["cancelled_call_count"] += 1
            self._active_spans.clear()
            self._summary_progress.clear()
            self._parents.clear()

    def build_run_usage(self) -> dict[str, Any]:
        """构造 Message 统计所需的稳定 Run 用量结构。"""
        with self._lock:
            total = self._usage.total
            model_count = (
                len(self._usage.by_model)
                if self._usage.by_model
                else len(self._model_names)
            )
            return {
                "trace_id": self.trace_id,
                "usage_id": self.trace_id,
                "model_count": model_count,
                "model_call_count": self._counters["model_call_count"],
                "tool_call_count": self._counters["tool_call_count"],
                "usage_source": "langchain_callback",
                "usage_present": self._usage.present,
                "input_tokens": total.input_tokens,
                "output_tokens": total.output_tokens,
                "total_tokens": (
                    total.total_tokens or total.input_tokens + total.output_tokens
                ),
                "input_details": dict(total.input_details),
                "output_details": dict(total.output_details),
                "models": {
                    model_name: usage.to_dict()
                    for model_name, usage in self._usage.by_model.items()
                },
            }

    def snapshot(self) -> dict[str, Any]:
        """返回可直接写入 JSONB 的平台观测快照。"""
        with self._lock:
            usage = self.build_run_usage()
            summary = {
                **self._counters,
                "model_count": usage["model_count"],
                "usage_present": usage["usage_present"],
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "total_tokens": usage["total_tokens"],
            }
            value: dict[str, Any] = {
                "schema_version": 1,
                "trace_id": self.trace_id,
                "status": self._status,
                "started_at": self._started_at.isoformat(),
                "finished_at": (
                    self._finished_at.isoformat()
                    if self._finished_at is not None
                    else None
                ),
                "duration_ms": self._duration_ms,
                "attributes": dict(self._attributes),
                "summary": summary,
                "usage": usage,
                "spans": [span.to_dict() for span in self._spans],
                "stages": list(self._stages),
                "events": list(self._events),
                "dropped": dict(self._dropped),
            }
            if self._error_type is not None:
                value["error_type"] = self._error_type
            return deepcopy(value)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """记录一次聊天模型的真实调用尝试。"""
        del messages, tags
        name, attributes = _model_identity(serialized, metadata, kwargs)
        self._start_span(
            run_id=run_id,
            parent_run_id=parent_run_id,
            category="model",
            kind="model",
            name=name,
            attributes=attributes,
        )
        self._summary_progress.start(run_id, metadata)

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """记录非聊天模型的真实调用尝试。"""
        del prompts, tags
        name, attributes = _model_identity(serialized, metadata, kwargs)
        self._start_span(
            run_id=run_id,
            parent_run_id=parent_run_id,
            category="model",
            kind="model",
            name=name,
            attributes=attributes,
        )
        self._summary_progress.start(run_id, metadata)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """结束模型 Span 并聚合 Provider Token 用量。"""
        del parent_run_id, tags, kwargs
        usage = normalize_llm_result(response)
        model_name = _response_model_name(response)
        with self._lock:
            span = self._active_spans.get(str(run_id))
            if span is None:
                return
            if model_name is None:
                model_name = _bounded_text(span.attributes.get("model_name"))
            if model_name is not None:
                self._model_names.add(model_name)
            if usage.present:
                self._usage.add(usage, model_name=model_name)
            # 消费活动调用与累计用量必须在同一把锁内，防止重复结束并发计费。
            self._finish_span(
                run_id,
                status="ok",
                usage=usage.to_dict() if usage.present else None,
                model_name=model_name,
            )
        self._summary_progress.finish(
            run_id, "completed", usage=usage.to_dict() if usage.present else None
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """结束失败或取消的模型 Span。"""
        del parent_run_id, tags, kwargs
        self._summary_progress.finish(
            run_id, "cancelled" if _exception_status(error) == "cancelled" else "failed"
        )
        self._finish_span(
            run_id,
            status=_exception_status(error),
            error_type=type(error).__name__,
        )

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """记录工具或 SubAgent 调度，不保留工具输入。"""
        del input_str, tags, kwargs
        name = _serialized_name(serialized)
        kind = "subagent" if name in _SUBAGENT_TOOL_NAMES else "tool"
        attributes = _safe_attributes(metadata, allowed_keys=_TOOL_METADATA_KEYS)
        attributes["tool_name"] = name
        if kind == "subagent" and isinstance(inputs, Mapping):
            subagent_name = inputs.get("subagent_type")
            if isinstance(subagent_name, str) and subagent_name:
                attributes["subagent_name"] = _bounded_text(subagent_name)
        self._start_span(
            run_id=run_id,
            parent_run_id=parent_run_id,
            category="tool",
            kind=kind,
            name=name,
            attributes=attributes,
        )

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """结束成功工具 Span，并识别框架返回的错误 ToolMessage。"""
        del parent_run_id, kwargs
        is_error = (
            isinstance(output, ToolMessage)
            and getattr(output, "status", "success") == "error"
        )
        self._finish_span(
            run_id,
            status="error" if is_error else "ok",
            error_type="ToolMessageError" if is_error else None,
            attributes=_async_subagent_attributes(output),
        )

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """结束失败或取消的工具 Span。"""
        del parent_run_id, kwargs
        self._finish_span(
            run_id,
            status=_exception_status(error),
            error_type=type(error).__name__,
        )

    def on_retry(
        self,
        retry_state: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """仅统计框架实际发出的重试回调，不推测未上报的重试。"""
        del retry_state, parent_run_id, kwargs
        with self._lock:
            self._counters["retry_count"] += 1
            span = self._active_spans.get(str(run_id))
            if span is not None:
                span.retry_number += 1

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """登记内部 Chain 父子关系，但不产生高噪音 Chain Span。"""
        del serialized, inputs, tags, metadata, kwargs
        with self._lock:
            self._parents[str(run_id)] = (
                str(parent_run_id) if parent_run_id is not None else None
            )

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """释放已完成 Chain 的临时父子关系。"""
        del outputs, parent_run_id, kwargs
        with self._lock:
            self._parents.pop(str(run_id), None)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """释放失败 Chain 的临时父子关系。"""
        del error, parent_run_id, kwargs
        with self._lock:
            self._parents.pop(str(run_id), None)

    def _start_span(
        self,
        *,
        run_id: UUID,
        parent_run_id: UUID | None,
        category: str,
        kind: str,
        name: str,
        attributes: dict[str, TelemetryValue],
    ) -> None:
        """创建一次真实调用；相同名称再次调用不等于发生重试。"""
        span_id = str(run_id)
        parent_id = str(parent_run_id) if parent_run_id is not None else None
        with self._lock:
            if span_id in self._active_spans:
                return
            self._parents[span_id] = parent_id
            if category == "model":
                self._counters["model_call_count"] += 1
                self._model_names.add(name)
            else:
                self._counters["tool_call_count"] += 1
                if kind == "subagent":
                    self._counters["subagent_call_count"] += 1
            span = _ObservedSpan(
                span_id=span_id,
                parent_span_id=self._nearest_observable_parent(parent_id),
                category=category,
                kind=kind,
                name=name,
                started_at=_utc_now(),
                started_monotonic=monotonic(),
                attributes=attributes,
            )
            self._active_spans[span_id] = span
            if len(self._spans) >= self._max_spans:
                self._dropped["spans"] += 1
                return
            self._spans.append(span)
            self._observable_spans[span_id] = span

    def _finish_span(
        self,
        run_id: UUID,
        *,
        status: str,
        error_type: str | None = None,
        usage: dict[str, Any] | None = None,
        model_name: str | None = None,
        attributes: Mapping[str, TelemetryValue] | None = None,
    ) -> None:
        """关闭活动调用并累计状态；Span 达到保存上限后仍保留汇总计数。"""
        span_id = str(run_id)
        with self._lock:
            span = self._active_spans.pop(span_id, None)
            self._parents.pop(span_id, None)
            if span is None:
                return
            if model_name is not None:
                span.name = model_name
                span.attributes["model_name"] = model_name
            if attributes:
                span.attributes.update(attributes)
            span.finish(status=status, error_type=error_type, usage=usage)
            if status == "error":
                self._counters["error_count"] += 1
            elif status == "cancelled":
                self._counters["cancelled_call_count"] += 1

    def _nearest_observable_parent(self, parent_id: str | None) -> str | None:
        """沿框架 Chain 关系寻找最近的模型、工具或 SubAgent Span。"""
        current = parent_id
        visited: set[str] = set()
        while current is not None and current not in visited:
            if current in self._observable_spans:
                return current
            visited.add(current)
            current = self._parents.get(current)
        return None


__all__ = ["PlatformAgentRunObserver"]
