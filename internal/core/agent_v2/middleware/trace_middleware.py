"""Agent V2 运行时可复用的模型和工具调用追踪中间件。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import uuid4

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

from internal.core.agent_v2.protocol.message_parser import response_messages
from internal.core.agent_v2.runtime.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.core.agent_v2.runtime.observability.token_usage import normalize_response


_span_stack: ContextVar[tuple[str, ...]] = ContextVar(
    "agent_v2_trace_span_stack",
    default=(),
)


@dataclass(frozen=True, slots=True)
class _ActiveSpan:
    """完成一次生命周期 Span 所需的内部状态。"""

    operation: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    started_at: float
    token: Any
    attributes: Mapping[str, str | int | float | bool | None]


class TraceMiddleware(AgentMiddleware):
    """在不改变执行语义的前提下追踪真实的模型和工具调用。

    中间件默认只记录标识和标量元数据，不把提示词、模型输出、
    工具参数和工具结果写入遥测数据。
    """

    def __init__(self, telemetry: AgentTelemetry | None = None) -> None:
        self._telemetry = telemetry

    def wrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """记录一次同步模型调用，包括失败情况。"""
        model_name = _model_name(getattr(request, "model", None))
        span = self._start_span(
            "model",
            model_name,
            request,
        )
        try:
            response = handler(request)
        except BaseException as exc:
            self._finish_span(
                span,
                status=_exception_status(exc),
                error_type=type(exc).__name__,
            )
            raise
        usage = normalize_response(response)
        self._finish_span(
            span,
            status="error" if _model_response_is_error(response) else "ok",
            attributes=usage.to_attributes() if usage.present else {},
        )
        return response

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """记录一次异步模型调用，包括取消情况。"""
        model_name = _model_name(getattr(request, "model", None))
        span = self._start_span(
            "model",
            model_name,
            request,
        )
        try:
            response = await handler(request)
        except BaseException as exc:
            self._finish_span(
                span,
                status=_exception_status(exc),
                error_type=type(exc).__name__,
            )
            raise
        usage = normalize_response(response)
        self._finish_span(
            span,
            status="error" if _model_response_is_error(response) else "ok",
            attributes=usage.to_attributes() if usage.present else {},
        )
        return response

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """记录一次同步工具调用，但不序列化业务载荷。"""
        span = self._start_span("tool", _tool_name(request), request)
        try:
            result = handler(request)
        except BaseException as exc:
            self._finish_span(
                span,
                status=_exception_status(exc),
                error_type=type(exc).__name__,
            )
            raise
        self._finish_span(
            span,
            status="error" if _tool_result_is_error(result) else "ok",
        )
        return result

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """记录一次异步工具调用，但不序列化业务载荷。"""
        span = self._start_span("tool", _tool_name(request), request)
        try:
            result = await handler(request)
        except BaseException as exc:
            self._finish_span(
                span,
                status=_exception_status(exc),
                error_type=type(exc).__name__,
            )
            raise
        self._finish_span(
            span,
            status="error" if _tool_result_is_error(result) else "ok",
        )
        return result

    def _start_span(
        self,
        operation: str,
        name: str,
        request: Any,
    ) -> _ActiveSpan:
        trace_id = _fallback_trace_id(request)
        stack = _span_stack.get()
        parent_span_id = stack[-1] if stack else None
        span_id = uuid4().hex
        attributes = {
            "operation": name,
            **_runtime_attributes(request),
        }
        if operation == "model":
            model = getattr(request, "model", None)
            attributes.update(
                {
                    "model_name": name,
                    "model_type": type(model).__name__ if model else None,
                    "model_provider": _model_provider(model),
                }
            )
        else:
            tool_call = getattr(request, "tool_call", {}) or {}
            call_id = tool_call.get("id")
            if call_id:
                attributes["tool_call_id"] = str(call_id)

        started_at = perf_counter()
        token = _span_stack.set((*stack, span_id))
        self._telemetry_for().emit_span(
            operation,
            phase="started",
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            attributes=attributes,
        )
        return _ActiveSpan(
            operation=operation,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            started_at=started_at,
            token=token,
            attributes=attributes,
        )

    def _finish_span(
        self,
        span: _ActiveSpan,
        *,
        status: str,
        error_type: str | None = None,
        attributes: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> None:
        duration_ms = (perf_counter() - span.started_at) * 1000
        finish_attributes = dict(span.attributes)
        finish_attributes.update(attributes or {})
        if error_type is not None:
            finish_attributes["error_type"] = error_type
        try:
            self._telemetry_for().emit_span(
                span.operation,
                phase="failed" if status != "ok" else "finished",
                trace_id=span.trace_id,
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                status=status,
                duration_ms=duration_ms,
                attributes=finish_attributes,
            )
        finally:
            _span_stack.reset(span.token)

    def _telemetry_for(self) -> AgentTelemetry:
        """返回当前图显式注入的遥测出口。"""
        return self._telemetry or NOOP_AGENT_TELEMETRY


def _fallback_trace_id(request: Any) -> str:
    """独立使用中间件时，优先使用框架运行 ID 作为 Trace ID。"""
    runtime = getattr(request, "runtime", None)
    execution_info = getattr(runtime, "execution_info", None)
    run_id = getattr(execution_info, "run_id", None)
    return str(run_id or uuid4())


def _runtime_attributes(request: Any) -> dict[str, str | None]:
    context = getattr(getattr(request, "runtime", None), "context", None)
    if context is None:
        return {}
    return {
        key: str(value) if value is not None else None
        for key in (
            "account_id",
            "app_id",
            "conversation_id",
            "principal_id",
            "memory_enabled",
            "agent_id",
        )
        if (value := getattr(context, key, None)) is not None
    }


def _model_name(model: Any) -> str:
    for attribute in ("model_name", "model", "model_id", "name"):
        value = getattr(model, attribute, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__ if model is not None else "unknown"


def _model_provider(model: Any) -> str | None:
    provider = getattr(model, "provider", None)
    if isinstance(provider, str) and provider:
        return provider
    module = getattr(type(model), "__module__", "")
    return module.split(".", 1)[0] if module else None


def _tool_name(request: Any) -> str:
    tool_call = getattr(request, "tool_call", {}) or {}
    name = tool_call.get("name")
    if name:
        return str(name)
    tool = getattr(request, "tool", None)
    return str(getattr(tool, "name", None) or type(tool).__name__)


def _model_response_is_error(response: Any) -> bool:
    for message in response_messages(response):
        if not isinstance(message, AIMessage):
            continue
        metadata = getattr(message, "response_metadata", None)
        if isinstance(metadata, Mapping) and metadata.get("error"):
            return True
    return False


def _tool_result_is_error(result: Any) -> bool:
    return (
        isinstance(result, ToolMessage)
        and getattr(result, "status", "success") == "error"
    )


def _exception_status(exc: BaseException) -> str:
    return (
        "cancelled"
        if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
        else "error"
    )


__all__ = ["TraceMiddleware"]
