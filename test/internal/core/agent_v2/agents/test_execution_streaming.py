"""GraphExecutionDriver 的 DeepAgents 0.6 流式事件测试。"""

from uuid import uuid4

from langchain_core.messages import AIMessageChunk, ToolMessage

from internal.core.agent_v2.agents.execution import GraphExecutionDriver
from internal.core.agent_v2.agents.models import QueueEvent
from internal.core.agent_v2.runtime.models import AgentRunContext


def _runtime_context() -> AgentRunContext:
    """构造不依赖外部服务的运行时上下文。"""
    return AgentRunContext(
        account_id=uuid4(),
        app_id=uuid4(),
        conversation_id=uuid4(),
        created_by=uuid4(),
        end_user_id=None,
    )


def test_driver_streams_model_deltas_and_todo_updates_once():
    """驱动器消费 messages/updates 并保持单一终止事件。"""
    events = []
    observed = {}
    todos = [
        {"content": "分析需求", "status": "completed"},
        {"content": "实现功能", "status": "in_progress"},
    ]

    class Graph:
        """返回一组 DeepAgents 0.6 风格的流事件。"""

        def stream(self, *_args, **kwargs):
            observed.update(kwargs)
            yield (
                "messages",
                (
                    AIMessageChunk(content="第一", id="message-1"),
                    {"checkpoint_ns": "model:1"},
                ),
            )
            yield (
                "messages",
                (
                    AIMessageChunk(content="段", id="message-1"),
                    {"checkpoint_ns": "model:1"},
                ),
            )
            yield (
                "messages",
                (
                    ToolMessage(content="todo updated", tool_call_id="todo-1"),
                    {"checkpoint_ns": "tools:1"},
                ),
            )
            yield (
                "updates",
                {
                    "tools": {
                        "todos": todos,
                        "messages": [
                            ToolMessage(
                                content="todo updated",
                                tool_call_id="todo-1",
                            )
                        ],
                    }
                },
            )

    GraphExecutionDriver(
        graph=Graph(),
        publisher=lambda _task_id, thought: events.append(thought),
        runtime_context=_runtime_context(),
    ).run(
        input_state={"messages": []},
        task_id=uuid4(),
        config=None,
        stream_options={},
    )

    assert [event.event for event in events] == [
        QueueEvent.AGENT_MESSAGE,
        QueueEvent.AGENT_MESSAGE,
        QueueEvent.TODO_UPDATE,
        QueueEvent.AGENT_END,
    ]
    assert [event.answer for event in events[:2]] == ["第一", "段"]
    assert events[0].id == events[1].id
    assert [todo.model_dump() for todo in events[2].todos] == todos
    assert observed["stream_mode"] == ["messages", "updates"]
