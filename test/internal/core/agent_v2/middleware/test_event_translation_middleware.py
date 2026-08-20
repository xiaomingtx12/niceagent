"""EventTranslationMiddleware 的队列事件边界测试。"""

from types import SimpleNamespace
from uuid import uuid4

from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.types import Command

from internal.core.agent_v2.agents.models import AgentType, QueueEvent
from internal.core.agent_v2.middleware.event_translation_middleware import (
    EventTranslationMiddleware,
    bind_event_translation,
)
from internal.core.agent_v2.events.serialization import serialize_agent_thought
from internal.core.agent_v2.events.translator import translate_model_chunk


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
    assert events[0].agent_id == "main"
    assert events[0].agent_type == AgentType.MAIN
    assert events[0].delegation_id == "call-1"
    assert events[1].tool == "researcher"
    assert events[1].agent_id == "researcher"
    assert events[1].agent_type == AgentType.SUBAGENT
    assert events[1].parent_agent_id == "main"
    assert events[1].delegation_id == "call-1"
    assert events[1].observation == "已完成调研并整理出三条结论"


def test_write_todos_emits_authoritative_todo_snapshot():
    """Successful native write_todos commands become one TODO_UPDATE event."""
    task_id = uuid4()
    events = []
    middleware = EventTranslationMiddleware()
    todos = [
        {"content": "分析需求", "status": "completed"},
        {"content": "实现功能", "status": "in_progress"},
    ]
    tool_call = {
        "id": "todo-1",
        "name": "write_todos",
        "args": {"todos": todos},
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
            lambda _request: Command(
                update={
                    "todos": todos,
                    "messages": [
                        ToolMessage(
                            content="Updated todo list",
                            tool_call_id="todo-1",
                        )
                    ],
                }
            ),
        )

    assert [event.event for event in events] == [QueueEvent.TODO_UPDATE]
    assert [todo.model_dump() for todo in events[0].todos] == todos
    assert events[0].tool_input == {"todos": todos}
    assert events[0].agent_id == "main"


def test_failed_write_todos_result_does_not_publish_snapshot():
    """Tool errors without a state update must not masquerade as Todo state."""
    task_id = uuid4()
    events = []
    middleware = EventTranslationMiddleware()
    tool_call = {
        "id": "todo-2",
        "name": "write_todos",
        "args": {"todos": []},
    }

    with bind_event_translation(
        task_id=task_id,
        publisher=lambda _task_id, thought: events.append(thought),
    ):
        middleware.wrap_tool_call(
            SimpleNamespace(tool_call=tool_call),
            lambda _request: ToolMessage(
                content="parallel update rejected",
                tool_call_id="todo-2",
                status="error",
            ),
        )

    assert events == []


def test_live_event_serializer_includes_p2_trace_fields():
    """Every streaming entrypoint receives one shared public event contract."""
    task_id = uuid4()
    events = []
    middleware = EventTranslationMiddleware()
    tool_call = {
        "id": "call-serialized",
        "name": "task",
        "args": {"subagent_type": "writer", "description": "整理结果"},
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

    payload = serialize_agent_thought(events[0])
    assert payload["agent_id"] == "main"
    assert payload["agent_type"] == "main"
    assert payload["delegation_id"] == "call-serialized"
    assert payload["todos"] == []


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
    assert events[1].observation == "search result"
    assert events[0].thought == "正在调用 search"


def test_error_event_is_terminal_and_keeps_the_observation_for_streaming():
    """运行错误可序列化为前端可展示的终止事件。"""
    task_id = uuid4()
    events = []

    with bind_event_translation(
        task_id=task_id,
        publisher=lambda _task_id, thought: events.append(thought),
    ) as session:
        session.publish_error("工具执行失败")
        session.finish()

    assert [event.event for event in events] == [QueueEvent.ERROR]
    assert events[0].observation == "工具执行失败"
    assert serialize_agent_thought(events[0])["observation"] == "工具执行失败"


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


def test_streamed_model_chunks_share_id_and_suppress_full_response():
    """messages 增量与模型完整响应只产生一条可追加消息。"""
    task_id = uuid4()
    events = []
    middleware = EventTranslationMiddleware()
    message_id = "streamed-message-1"

    with bind_event_translation(
        task_id=task_id,
        publisher=lambda _task_id, thought: events.append(thought),
        stream_model_messages=True,
    ) as session:
        for content in ["第一", "个答案"]:
            thought = translate_model_chunk(
                session,
                AIMessageChunk(content=content, id=message_id),
            )
            assert thought is not None
            session.publish(thought)

        middleware.wrap_model_call(
            None,
            lambda _request: ModelResponse(
                result=[AIMessage(content="第一个答案", id=message_id)]
            ),
        )
        middleware.after_model({}, None)
        session.finish()

    message_events = [
        event for event in events if event.event == QueueEvent.AGENT_MESSAGE
    ]
    assert [event.answer for event in message_events] == ["第一", "个答案"]
    assert len({event.id for event in message_events}) == 1
    assert events[-1].event == QueueEvent.AGENT_END
