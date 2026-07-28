"""把 LangGraph update 中的消息转换成 MyDify 队列事件。

事件转换发生在 ``CompiledStateGraph.stream(stream_mode="updates")`` 的消费端，
不再依赖 middleware 原地修改 ``request.state``。LangGraph 不保证这类原地修改
会成为 node update，旧实现因此可能只发出 ``agent_end`` 而丢失最终答案。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeAlias
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, ToolMessage

from internal.core.agent.entities.queue_entity import AgentThought, QueueEvent

ToolCallContext: TypeAlias = dict[str, tuple[str, dict[str, Any]]]


def translate_ai_message(
    message: AIMessage,
    *,
    task_id: UUID,
) -> tuple[list[AgentThought], ToolCallContext]:
    """转换模型输出，并返回供后续 ToolMessage 关联的 tool_call 上下文。"""
    content = _message_text(message.content)
    tool_context: ToolCallContext = {}
    thoughts: list[AgentThought] = []

    if not message.tool_calls:
        thoughts.append(
            AgentThought(
                id=uuid4(),
                task_id=task_id,
                event=QueueEvent.AGENT_MESSAGE,
                answer=content,
                message_token_count=_usage_value(message, "input_tokens"),
                answer_token_count=_usage_value(message, "output_tokens"),
                total_token_count=_usage_value(message, "total_tokens"),
            )
        )
        return thoughts, tool_context

    for tool_call in message.tool_calls:
        tool_name = str(tool_call.get("name", ""))
        tool_input = dict(tool_call.get("args", {}) or {})
        tool_call_id = str(tool_call.get("id", ""))
        if tool_call_id:
            tool_context[tool_call_id] = (tool_name, tool_input)
        is_subagent = tool_name == "task"
        thoughts.append(
            AgentThought(
                id=uuid4(),
                task_id=task_id,
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
    return thoughts, tool_context


def translate_tool_message(
    message: ToolMessage,
    *,
    task_id: UUID,
    tool_calls: ToolCallContext,
) -> AgentThought:
    """转换工具结果，并通过 tool_call_id 恢复工具名和参数。"""
    tool_name, tool_input = tool_calls.get(
        str(message.tool_call_id),
        (str(message.name or ""), {}),
    )
    is_subagent = tool_name == "task"
    return AgentThought(
        id=uuid4(),
        task_id=task_id,
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
    """归一化字符串和标准 LangChain content blocks。"""
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
    "ToolCallContext",
    "translate_ai_message",
    "translate_tool_message",
]
