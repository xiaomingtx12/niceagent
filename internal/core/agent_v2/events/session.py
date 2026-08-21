"""单次运行的事件发布与工具调用关联。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, TypeAlias
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from internal.core.agent_v2.agents.models import AgentThought, QueueEvent
from internal.core.agent_v2.runtime.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.core.agent_v2.runtime.token_usage import (
    TokenUsage,
    TokenUsageAccumulator,
)

EventPublisher: TypeAlias = Callable[[UUID, AgentThought], None]
CancellationProbe: TypeAlias = Callable[[], bool]


@dataclass(frozen=True)
class ToolCallRecord:
    """关联模型与工具事件的规范调用元数据。"""

    call_id: str
    name: str
    arguments: dict[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ToolCallRecord":
        """把 LangChain 工具调用规范为稳定记录。"""
        tool_call = dict(value or {})
        return cls(
            call_id=str(tool_call.get("id", "")),
            name=str(tool_call.get("name", "")),
            arguments=dict(tool_call.get("args", {}) or {}),
        )


@dataclass
class EventTranslationSession:
    """保存单次图调用的隔离事件状态。"""

    task_id: UUID
    publisher: EventPublisher
    telemetry: AgentTelemetry = NOOP_AGENT_TELEMETRY
    cancelled: CancellationProbe = lambda: False
    tool_calls: dict[str, ToolCallRecord] = field(default_factory=dict)
    pending_model_events: list[tuple[Any, AgentThought]] = field(
        default_factory=list
    )
    observed_model_messages: set[tuple[str, str]] = field(default_factory=set)
    model_event_ids: dict[tuple[str, str], UUID] = field(default_factory=dict)
    streamed_model_messages: set[tuple[str, str]] = field(default_factory=set)
    todo_call_ids: set[str] = field(default_factory=set)
    stream_model_messages: bool = False
    terminal_published: bool = False
    _last_event_at: float = field(default_factory=perf_counter)
    trace_id: UUID = field(default_factory=uuid4)
    token_usage: TokenUsageAccumulator = field(
        default_factory=TokenUsageAccumulator
    )
    model_call_count: int = 0
    tool_call_count: int = 0

    def remember_tool_call(self, tool_call: ToolCallRecord) -> None:
        """记录带框架关联标识的工具调用。"""
        if tool_call.call_id:
            self.tool_calls[tool_call.call_id] = tool_call

    def resolve_tool_call(
        self,
        call_id: str,
        fallback: ToolCallRecord,
    ) -> ToolCallRecord:
        """把工具结果关联到原模型请求。"""
        return self.tool_calls.get(call_id, fallback)

    @staticmethod
    def _message_key(
        message: Any,
        stream_key: str | None = None,
    ) -> tuple[str, str]:
        """生成一次运行内稳定的消息标识。"""
        message_id = str(getattr(message, "id", "") or "")
        if message_id:
            return ("id", message_id)
        if stream_key:
            return ("stream", stream_key)
        return ("content", repr(getattr(message, "content", "")))

    def remember_model_message(
        self,
        message: Any,
        stream_key: str | None = None,
    ) -> None:
        """记录已经由模型调用翻译的消息。"""
        self.observed_model_messages.add(self._message_key(message, stream_key))

    def has_seen_model_message(
        self,
        message: Any,
        stream_key: str | None = None,
    ) -> bool:
        """检查消息是否已经进入模型事件队列。"""
        return self._message_key(message, stream_key) in self.observed_model_messages

    def model_event_id(
        self,
        message: Any,
        stream_key: str | None = None,
    ) -> UUID:
        """为一次模型响应生成稳定的业务事件 ID。"""
        key = self._message_key(message, stream_key)
        event_id = self.model_event_ids.get(key)
        if event_id is None:
            event_id = uuid5(
                NAMESPACE_URL,
                f"mydify-agent-message:{self.task_id}:{key[0]}:{key[1]}",
            )
            self.model_event_ids[key] = event_id
        return event_id

    def mark_model_message_streamed(
        self,
        message: Any,
        stream_key: str | None = None,
    ) -> None:
        """记录模型响应已经通过 messages 流输出。"""
        key = self._message_key(message, stream_key)
        self.remember_model_message(message, stream_key)
        self.streamed_model_messages.add(key)

    def has_streamed_model_message(
        self,
        message: Any,
        stream_key: str | None = None,
    ) -> bool:
        """检查完整模型消息是否已经由增量事件覆盖。"""
        return self._message_key(message, stream_key) in self.streamed_model_messages

    def remember_todo_call(self, call_id: str) -> bool:
        """记录 Todo 调用并返回是否应首次发布。"""
        if not call_id:
            return True
        if call_id in self.todo_call_ids:
            return False
        self.todo_call_ids.add(call_id)
        return True

    def queue_model_events(
        self,
        message: Any,
        thoughts: list[AgentThought],
    ) -> None:
        """等待内部审核通过后再发布模型事件。"""
        self.remember_model_message(message)
        self.pending_model_events.extend((message, thought) for thought in thoughts)

    def flush_model_events(self, *, final: bool = False) -> None:
        """按原顺序发布已接受的模型事件。"""
        pending = self.pending_model_events
        self.pending_model_events = []
        deferred: list[tuple[Any, AgentThought]] = []
        for message, thought in pending:
            if thought.event == QueueEvent.AGENT_MESSAGE:
                if self.stream_model_messages and not final:
                    deferred.append((message, thought))
                    continue
                if self.has_streamed_model_message(message):
                    continue
            self.publish(thought)
        self.pending_model_events = deferred + self.pending_model_events

    def publish(self, thought: AgentThought) -> None:
        """发布事件并附加距上一事件的耗时。"""
        if self.terminal_published:
            return
        if self.cancelled():
            self.cancel()
            return
        now = perf_counter()
        timed_thought = thought.model_copy(
            update={"latency": max(0.0, now - self._last_event_at)}
        )
        self._last_event_at = now
        self.publisher(self.task_id, timed_thought)
        self.telemetry.emit(
            "agent.event",
            attributes={
                "trace_id": str(self.trace_id),
                "task_id": str(self.task_id),
                "agent_type": timed_thought.agent_type.value,
                "event": timed_thought.event.value,
                "latency_ms": round(timed_thought.latency * 1000, 3),
                "tool": timed_thought.tool or None,
                "total_tokens": timed_thought.total_token_count,
            },
        )
        if timed_thought.event in TERMINAL_EVENTS:
            self.terminal_published = True

    def publish_error(self, error: Exception | str) -> None:
        """发布本次调用的首个终止错误。"""
        if self.terminal_published:
            return
        self.publish(
            AgentThought(
                id=uuid4(),
                task_id=self.task_id,
                event=QueueEvent.ERROR,
                observation=str(error),
            )
        )

    def record_model_usage(
        self,
        usage: TokenUsage,
        *,
        model_name: str | None = None,
    ) -> None:
        """记录一次真实模型调用的归一化用量。"""
        self.token_usage.add(usage, model_name=model_name)

    def record_model_call(self) -> None:
        """记录一次真实模型调用，即使 Provider 没有返回 Token。"""
        self.model_call_count += 1

    def record_tool_call(self) -> None:
        """记录一次真实工具调用。"""
        self.tool_call_count += 1

    def build_token_usage_payload(
        self,
        framework_usage: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """构建一次运行的统一用量汇总，兼容回调和 Trace 两种来源。"""
        aggregate = TokenUsageAccumulator()
        source = "trace_span"
        if framework_usage:
            aggregate.add_mapping(
                framework_usage,
                source="langchain_callback",
            )
            if aggregate.present:
                source = "langchain_callback"
        if not aggregate.present:
            aggregate = self.token_usage

        if not aggregate.present and not self.model_call_count and not self.tool_call_count:
            return None

        total = aggregate.total
        return {
            "trace_id": str(self.trace_id),
            "task_id": str(self.task_id),
            "model_count": len(aggregate.by_model),
            "model_call_count": self.model_call_count or aggregate.call_count,
            "tool_call_count": self.tool_call_count,
            "usage_source": source,
            "usage_present": aggregate.present,
            "input_tokens": total.input_tokens,
            "output_tokens": total.output_tokens,
            "total_tokens": total.total_tokens
            or total.input_tokens + total.output_tokens,
            "input_details": dict(total.input_details),
            "output_details": dict(total.output_details),
            "models": {
                model_name: usage.to_dict()
                for model_name, usage in aggregate.by_model.items()
            },
        }

    def publish_token_usage(
        self,
        framework_usage: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """发布一次运行聚合结果，优先采用 LangChain 回调总量。"""
        payload = self.build_token_usage_payload(framework_usage)
        if payload is None:
            return None

        run_attributes: dict[str, Any] = {
            key: value
            for key, value in payload.items()
            if key not in {"input_details", "output_details", "models"}
        }
        run_attributes.update(
            {
                f"input_{key}_tokens": value
                for key, value in payload["input_details"].items()
            }
        )
        run_attributes.update(
            {
                f"output_{key}_tokens": value
                for key, value in payload["output_details"].items()
            }
        )
        self.telemetry.emit(
            "agent.run.token_usage",
            attributes=run_attributes,
        )
        for model_name, usage in payload["models"].items():
            model_attributes: dict[str, Any] = {
                "trace_id": str(self.trace_id),
                "task_id": str(self.task_id),
                "model_name": model_name,
                "usage_source": payload["usage_source"],
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "total_tokens": usage["total_tokens"],
            }
            for direction in ("input", "output"):
                for detail_name, value in usage[f"{direction}_details"].items():
                    suffix = str(detail_name)
                    if not suffix.endswith("_tokens"):
                        suffix = f"{suffix}_tokens"
                    model_attributes[f"{direction}_{suffix}"] = value
            self.telemetry.emit(
                "agent.model.token_usage",
                attributes=model_attributes,
            )
        return payload

    def finish(self) -> None:
        """缺少终止事件时发布一次正常结束。"""
        if self.cancelled():
            self.cancel()
            return
        if self.terminal_published:
            return
        self.flush_model_events(final=True)
        self.publish(
            AgentThought(
                id=uuid4(),
                task_id=self.task_id,
                event=QueueEvent.AGENT_END,
            )
        )

    def cancel(self) -> None:
        """标记取消并丢弃尚未发布的模型事件。"""
        self.pending_model_events = []
        self.terminal_published = True


TERMINAL_EVENTS = {
    QueueEvent.AGENT_END,
    QueueEvent.ERROR,
    QueueEvent.STOP,
    QueueEvent.TIMEOUT,
}
_current_session: ContextVar[EventTranslationSession | None] = ContextVar(
    "deepagents_event_translation_session",
    default=None,
)


def current_event_session() -> EventTranslationSession | None:
    """返回当前上下文绑定的事件会话。"""
    return _current_session.get()


def is_current_run_cancelled() -> bool:
    """检查当前图运行是否已收到取消信号。"""
    session = current_event_session()
    return session.cancelled() if session is not None else False


@contextmanager
def bind_event_translation(
    *,
    task_id: UUID,
    publisher: EventPublisher,
    telemetry: AgentTelemetry | None = None,
    cancelled: CancellationProbe | None = None,
    stream_model_messages: bool = False,
) -> Iterator[EventTranslationSession]:
    """把一次图调用绑定到 MyDify 事件发布器。"""
    session = EventTranslationSession(
        task_id=task_id,
        publisher=publisher,
        telemetry=telemetry or NOOP_AGENT_TELEMETRY,
        cancelled=cancelled or (lambda: False),
        stream_model_messages=stream_model_messages,
    )
    token = _current_session.set(session)
    try:
        yield session
    finally:
        _current_session.reset(token)


__all__ = [
    "EventTranslationSession",
    "ToolCallRecord",
    "bind_event_translation",
    "current_event_session",
    "is_current_run_cancelled",
]
