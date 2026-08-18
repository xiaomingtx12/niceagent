"""MyDify 业务事件的 LangChain 中间件适配器。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from internal.core.agent_v2.events.session import (
    EventTranslationSession,
    ToolCallRecord,
    bind_event_translation,
    current_event_session,
)
from internal.core.agent_v2.events.translator import (
    response_messages,
    translate_ai_message,
    translate_tool_result,
)


class EventTranslationMiddleware(AgentMiddleware):
    """将模型和工具钩子接入业务事件发布器。"""

    def wrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """暂存通过内部审核的模型事件。"""
        response = handler(request)
        session = current_event_session()
        if session is None:
            return response
        for message in response_messages(response):
            if isinstance(message, AIMessage):
                session.queue_model_events(
                    message,
                    translate_ai_message(session, message),
                )
        return response

    def after_model(self, state: dict, runtime: Any) -> None:
        """内部模型钩子完成后发布事件。"""
        session = current_event_session()
        if session is not None:
            session.flush_model_events()
        return None

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """关联工具结果并发布业务事件。"""
        fallback = ToolCallRecord.from_mapping(
            getattr(request, "tool_call", {}) or {}
        )
        session = current_event_session()
        if session is not None:
            session.remember_tool_call(fallback)
        result = handler(request)
        if session is not None:
            for thought in translate_tool_result(session, result, fallback):
                session.publish(thought)
        return result

    def after_agent(self, state: dict, runtime: Any) -> None:
        """图执行完成后发布正常终止事件。"""
        session = current_event_session()
        if session is not None:
            messages = list(state.get("messages", []) or [])
            final_message = next(
                (
                    message
                    for message in reversed(messages)
                    if isinstance(message, AIMessage)
                ),
                None,
            )
            if (
                final_message is not None
                and not session.has_seen_model_message(final_message)
            ):
                for thought in translate_ai_message(session, final_message):
                    session.publish(thought)
            session.finish()
        return None


__all__ = [
    "EventTranslationMiddleware",
    "EventTranslationSession",
    "bind_event_translation",
]
