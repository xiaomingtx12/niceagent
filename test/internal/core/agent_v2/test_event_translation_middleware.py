"""LangGraph update 消息到 MyDify 队列事件的转换测试。"""

from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage

from internal.core.agent.entities.queue_entity import QueueEvent
from internal.core.agent_v2.middleware.event_translation_middleware import (
    translate_ai_message,
    translate_tool_message,
)


def test_task_tool_call_emits_subagent_boundaries():
    task_id = uuid4()
    ai_message = AIMessage(
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
    )

    thoughts, tool_calls = translate_ai_message(ai_message, task_id=task_id)
    result = translate_tool_message(
        ToolMessage(
            content="已完成调研并整理出三条结论",
            tool_call_id="call-1",
        ),
        task_id=task_id,
        tool_calls=tool_calls,
    )

    assert thoughts[0].event == QueueEvent.SUB_AGENT_DELEGATE
    assert thoughts[0].tool == "researcher"
    assert result.event == QueueEvent.SUB_AGENT_END
    assert result.tool == "researcher"
    assert result.observation == "已完成调研并整理出三条结论"


def test_ordinary_tool_keeps_agent_action_event():
    task_id = uuid4()
    ai_message = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call-2",
                "name": "search",
                "args": {"query": "deepagents"},
            }
        ],
    )

    thoughts, tool_calls = translate_ai_message(ai_message, task_id=task_id)
    result = translate_tool_message(
        ToolMessage(content="search result", tool_call_id="call-2"),
        task_id=task_id,
        tool_calls=tool_calls,
    )

    assert thoughts[0].event == QueueEvent.AGENT_ACTION
    assert result.event == QueueEvent.AGENT_ACTION
    assert result.tool == "search"
    assert result.tool_input == {"query": "deepagents"}


def test_text_response_emits_agent_message_with_answer_and_usage():
    thoughts, tool_calls = translate_ai_message(
        AIMessage(
            content=[{"type": "text", "text": "最终答案"}],
            usage_metadata={
                "input_tokens": 7,
                "output_tokens": 3,
                "total_tokens": 10,
            },
        ),
        task_id=uuid4(),
    )

    assert tool_calls == {}
    assert thoughts[0].event == QueueEvent.AGENT_MESSAGE
    assert thoughts[0].answer == "最终答案"
    assert thoughts[0].message_token_count == 7
    assert thoughts[0].answer_token_count == 3
