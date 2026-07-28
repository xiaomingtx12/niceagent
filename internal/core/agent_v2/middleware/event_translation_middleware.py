"""DeepAgents/LangChain 事件到 MyDify SSE 事件的唯一翻译边界。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, TypeAlias
from uuid import UUID, uuid4

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.types import Command

from internal.core.agent.entities.queue_entity import AgentThought, QueueEvent

EventPublisher: TypeAlias = Callable[[UUID, AgentThought], None]
ToolCallContext: TypeAlias = dict[str, tuple[str, dict[str, Any]]]


@dataclass
class EventTranslationSession:
    """一次 graph 调用的事件上下文，借助 ContextVar 隔离并发请求。"""

    task_id: UUID
    publisher: EventPublisher
    tool_calls: ToolCallContext = field(default_factory=dict)
    pending_model_events: list[AgentThought] = field(default_factory=list)
    terminal_published: bool = False
    _last_event_at: float = field(default_factory=perf_counter)

    def publish(self, thought: AgentThought) -> None:
        now = perf_counter()
        thought = thought.model_copy(
            update={"latency": max(0.0, now - self._last_event_at)}
        )
        self._last_event_at = now
        self.publisher(self.task_id, thought)
        if thought.event in _TERMINAL_EVENTS:
            self.terminal_published = True

    def publish_error(self, error: Exception | str) -> None:
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
        if self.terminal_published:
            return
        self.publish(
            AgentThought(
                id=uuid4(),
                task_id=self.task_id,
                event=QueueEvent.AGENT_END,
            )
        )


_TERMINAL_EVENTS = {
    QueueEvent.AGENT_END,
    QueueEvent.ERROR,
    QueueEvent.STOP,
    QueueEvent.TIMEOUT,
}
_current_session: ContextVar[EventTranslationSession | None] = ContextVar(
    "deepagents_event_translation_session",
    default=None,
)


@contextmanager
def bind_event_translation(
    *,
    task_id: UUID,
    publisher: EventPublisher,
) -> Iterator[EventTranslationSession]:
    """把当前执行线程绑定到对应的 MyDify SSE 队列。"""
    session = EventTranslationSession(task_id=task_id, publisher=publisher)
    token = _current_session.set(session)
    try:
        yield session
    finally:
        _current_session.reset(token)


class EventTranslationMiddleware(AgentMiddleware):
    """在 AgentMiddleware 钩子内发布模型、工具和结束事件。"""

    state_schema = Any

    def wrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        response = handler(request)
        session = _current_session.get()
        if session is None:
            return response

        for message in _response_messages(response):
            if isinstance(message, AIMessage):
                session.pending_model_events.extend(
                    self._translate_ai_message(session, message)
                )
        return response

    def after_model(self, state: dict, runtime: Any) -> None:
        """审核等内层 after_model 钩子通过后，才把模型输出发给客户端。"""
        session = _current_session.get()
        if session is None:
            return None
        pending = session.pending_model_events
        session.pending_model_events = []
        for thought in pending:
            session.publish(thought)
        return None

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        result = handler(request)
        session = _current_session.get()
        if session is None:
            return result

        tool_call = dict(getattr(request, "tool_call", {}) or {})
        tool_call_id = str(tool_call.get("id", ""))
        tool_name = str(tool_call.get("name", ""))
        tool_input = dict(tool_call.get("args", {}) or {})
        if tool_call_id:
            session.tool_calls.setdefault(tool_call_id, (tool_name, tool_input))

        for message in _tool_messages(result):
            session.publish(_tool_thought(session, message, tool_name, tool_input))
        return result

    def after_agent(self, state: dict, runtime: Any) -> None:
        session = _current_session.get()
        if session is not None:
            session.finish()
        return None

    @staticmethod
    def _translate_ai_message(
        session: EventTranslationSession,
        message: AIMessage,
    ) -> list[AgentThought]:
        content = _message_text(message.content)
        if not message.tool_calls:
            return [
                AgentThought(
                    id=uuid4(),
                    task_id=session.task_id,
                    event=QueueEvent.AGENT_MESSAGE,
                    answer=content,
                    message_token_count=_usage_value(message, "input_tokens"),
                    answer_token_count=_usage_value(message, "output_tokens"),
                    total_token_count=_usage_value(message, "total_tokens"),
                )
            ]

        thoughts: list[AgentThought] = []
        for tool_call in message.tool_calls:
            tool_name = str(tool_call.get("name", ""))
            tool_input = dict(tool_call.get("args", {}) or {})
            tool_call_id = str(tool_call.get("id", ""))
            if tool_call_id:
                session.tool_calls[tool_call_id] = (tool_name, tool_input)
            is_subagent = tool_name == "task"
            thoughts.append(
                AgentThought(
                    id=uuid4(),
                    task_id=session.task_id,
                    event=(
                        QueueEvent.SUB_AGENT_DELEGATE
                        if is_subagent
                        else QueueEvent.AGENT_ACTION
                    ),
                    thought=content,
                    tool=(
                        str(tool_input.get("subagent_type", "task"))
                        if is_subagent
                        else tool_name
                    ),
                    tool_input=tool_input,
                    message_token_count=_usage_value(message, "input_tokens"),
                    answer_token_count=_usage_value(message, "output_tokens"),
                    total_token_count=_usage_value(message, "total_tokens"),
                )
            )
        return thoughts


def _response_messages(response: Any) -> list[BaseMessage]:
    model_response = getattr(response, "model_response", response)
    result = getattr(model_response, "result", model_response)
    if isinstance(result, BaseMessage):
        return [result]
    if isinstance(result, list):
        return [message for message in result if isinstance(message, BaseMessage)]
    return []


def _tool_messages(result: ToolMessage | Command[Any]) -> list[ToolMessage]:
    if isinstance(result, ToolMessage):
        return [result]
    if not isinstance(result, Command):
        return []
    update = result.update
    if not isinstance(update, Mapping):
        return []
    messages = update.get("messages", [])
    if not isinstance(messages, list):
        messages = [messages]
    return [message for message in messages if isinstance(message, ToolMessage)]


def _tool_thought(
    session: EventTranslationSession,
    message: ToolMessage,
    fallback_name: str,
    fallback_input: dict[str, Any],
) -> AgentThought:
    tool_name, tool_input = session.tool_calls.get(
        str(message.tool_call_id),
        (fallback_name or str(message.name or ""), fallback_input),
    )
    is_subagent = tool_name == "task"
    return AgentThought(
        id=uuid4(),
        task_id=session.task_id,
        event=QueueEvent.SUB_AGENT_END if is_subagent else QueueEvent.AGENT_ACTION,
        tool=(
            str(tool_input.get("subagent_type", "task"))
            if is_subagent
            else tool_name
        ),
        tool_input=tool_input,
        observation=_message_text(message.content),
    )


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _usage_value(message: AIMessage, key: str) -> int:
    usage = message.usage_metadata
    if isinstance(usage, Mapping):
        return int(usage.get(key, 0) or 0)
    return int(getattr(usage, key, 0) or 0)


__all__ = [
    "EventTranslationMiddleware",
    "EventTranslationSession",
    "bind_event_translation",
]
