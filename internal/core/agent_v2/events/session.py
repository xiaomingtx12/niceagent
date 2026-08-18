"""单次运行的事件发布与工具调用关联。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, TypeAlias
from uuid import UUID, uuid4

from internal.core.agent_v2.agents.models import AgentThought, QueueEvent
from internal.core.agent_v2.runtime.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
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
    pending_model_events: list[AgentThought] = field(default_factory=list)
    observed_model_messages: set[tuple[str, str]] = field(default_factory=set)
    terminal_published: bool = False
    _last_event_at: float = field(default_factory=perf_counter)

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
    def _message_key(message: Any) -> tuple[str, str]:
        """生成一次运行内稳定的消息标识。"""
        return (
            str(getattr(message, "id", "") or ""),
            repr(getattr(message, "content", "")),
        )

    def remember_model_message(self, message: Any) -> None:
        """记录已经由模型调用翻译的消息。"""
        self.observed_model_messages.add(self._message_key(message))

    def has_seen_model_message(self, message: Any) -> bool:
        """检查消息是否已经进入模型事件队列。"""
        return self._message_key(message) in self.observed_model_messages

    def queue_model_events(
        self,
        message: Any,
        thoughts: list[AgentThought],
    ) -> None:
        """等待内部审核通过后再发布模型事件。"""
        self.remember_model_message(message)
        self.pending_model_events.extend(thoughts)

    def flush_model_events(self) -> None:
        """按原顺序发布已接受的模型事件。"""
        pending = self.pending_model_events
        self.pending_model_events = []
        for thought in pending:
            self.publish(thought)

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

    def finish(self) -> None:
        """缺少终止事件时发布一次正常结束。"""
        if self.cancelled():
            self.cancel()
            return
        if self.terminal_published:
            return
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
) -> Iterator[EventTranslationSession]:
    """把一次图调用绑定到 MyDify 事件发布器。"""
    session = EventTranslationSession(
        task_id=task_id,
        publisher=publisher,
        telemetry=telemetry or NOOP_AGENT_TELEMETRY,
        cancelled=cancelled or (lambda: False),
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
