"""DeepAgents task-tool boundaries map to explicit multi-agent events."""

from types import SimpleNamespace
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from internal.core.agent.entities.queue_entity import QueueEvent
from internal.core.agent_v2.middleware.event_translation_middleware import (
    EventTranslationMiddleware,
)


def test_task_tool_call_emits_subagent_delegate():
    task_id = uuid4()
    state = {"task_id": task_id, "agent_thoughts": []}
    request = SimpleNamespace(state=state)
    response = SimpleNamespace(
        result=AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call-1",
                    "name": "task",
                    "args": {
                        "subagent_type": "researcher",
                        "description": "调查 DeepAgents 的最新行为",
                    },
                }
            ],
            usage_metadata={
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        )
    )

    middleware = EventTranslationMiddleware(task_id=task_id)
    middleware.wrap_model_call(request, lambda _: response)

    thought = state["agent_thoughts"][0]
    assert thought.event == QueueEvent.SUB_AGENT_DELEGATE
    assert thought.tool == "researcher"
    assert thought.tool_input["description"] == "调查 DeepAgents 的最新行为"


def test_task_command_result_emits_subagent_end():
    task_id = uuid4()
    state = {"task_id": task_id, "agent_thoughts": []}
    request = SimpleNamespace(
        state=state,
        tool_call={
            "id": "call-1",
            "name": "task",
            "args": {
                "subagent_type": "researcher",
                "description": "调查框架",
            },
        },
    )
    result = Command(
        update={
            "messages": [
                ToolMessage(
                    content="已完成调研并整理出三条结论",
                    tool_call_id="call-1",
                )
            ]
        }
    )

    middleware = EventTranslationMiddleware(task_id=task_id)
    middleware.wrap_tool_call(request, lambda _: result)

    thought = state["agent_thoughts"][0]
    assert thought.event == QueueEvent.SUB_AGENT_END
    assert thought.tool == "researcher"
    assert thought.observation == "已完成调研并整理出三条结论"


def test_ordinary_tool_keeps_agent_action_event():
    task_id = uuid4()
    state = {"task_id": task_id, "agent_thoughts": []}
    request = SimpleNamespace(
        state=state,
        tool_call={"id": "call-2", "name": "search", "args": {"query": "deepagents"}},
    )
    result = ToolMessage(content="search result", tool_call_id="call-2")

    middleware = EventTranslationMiddleware(task_id=task_id)
    middleware.wrap_tool_call(request, lambda _: result)

    assert state["agent_thoughts"][0].event == QueueEvent.AGENT_ACTION
