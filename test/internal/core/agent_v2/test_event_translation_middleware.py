"""EventTranslationMiddleware 的队列事件边界测试。"""

from types import SimpleNamespace
from uuid import uuid4

from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, ToolMessage

from internal.core.agent.entities.queue_entity import QueueEvent
from internal.core.agent_v2.middleware.event_translation_middleware import (
    EventTranslationMiddleware,
    bind_event_translation,
)


def test_task_tool_call_emits_subagent_boundaries():
    task_id = uuid4()
    events = []
    middleware = EventTranslationMiddleware()
    tool_call = {
        "id": "call-1",
        "name": "task",
        "args": {
            "subagent_type": "researcher",
            "description": "调查 DeepAgents 的最新行为",
        },
    }

    with bind_event_translation(
        task_id=task_id,
        publisher=lambda _task_id, thought: events.append(thought),
    ):
        middleware.wrap_model_call(
            None,
            lambda _request: ModelResponse(
                result=[AIMessage(content="", tool_calls=[tool_call])]
            ),
        )
        middleware.after_model({}, None)
        middleware.wrap_tool_call(
            SimpleNamespace(tool_call=tool_call),
            lambda _request: ToolMessage(
                content="已完成调研并整理出三条结论",
                tool_call_id="call-1",
            ),
        )

    assert [event.event for event in events] == [
        QueueEvent.SUB_AGENT_DELEGATE,
        QueueEvent.SUB_AGENT_END,
    ]
    assert events[0].tool == "researcher"
    assert events[1].tool == "researcher"
    assert events[1].observation == "已完成调研并整理出三条结论"


def test_ordinary_tool_keeps_agent_action_event():
    task_id = uuid4()
    events = []
    middleware = EventTranslationMiddleware()
    tool_call = {
        "id": "call-2",
        "name": "search",
        "args": {"query": "deepagents"},
    }

    with bind_event_translation(
        task_id=task_id,
        publisher=lambda _task_id, thought: events.append(thought),
    ):
        middleware.wrap_model_call(
            None,
            lambda _request: ModelResponse(
                result=[AIMessage(content="", tool_calls=[tool_call])]
            ),
        )
        middleware.after_model({}, None)
        middleware.wrap_tool_call(
            SimpleNamespace(tool_call=tool_call),
            lambda _request: ToolMessage(
                content="search result",
                tool_call_id="call-2",
            ),
        )

    assert [event.event for event in events] == [
        QueueEvent.AGENT_ACTION,
        QueueEvent.AGENT_ACTION,
    ]
    assert events[1].tool == "search"
    assert events[1].tool_input == {"query": "deepagents"}


def test_text_response_emits_agent_message_with_answer_and_usage():
    task_id = uuid4()
    events = []
    middleware = EventTranslationMiddleware()

    with bind_event_translation(
        task_id=task_id,
        publisher=lambda _task_id, thought: events.append(thought),
    ) as session:
        middleware.wrap_model_call(
            None,
            lambda _request: ModelResponse(
                result=[
                    AIMessage(
                        content=[{"type": "text", "text": "最终答案"}],
                        usage_metadata={
                            "input_tokens": 7,
                            "output_tokens": 3,
                            "total_tokens": 10,
                        },
                    )
                ]
            ),
        )
        assert events == []
        middleware.after_model({}, None)
        session.finish()
        session.finish()

    assert [event.event for event in events] == [
        QueueEvent.AGENT_MESSAGE,
        QueueEvent.AGENT_END,
    ]
    assert events[0].answer == "最终答案"
    assert events[0].message_token_count == 7
    assert events[0].answer_token_count == 3
