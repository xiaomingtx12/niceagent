"""LangChain 消息到 MyDify 业务事件的纯翻译。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.types import Command

from internal.core.agent_v2.agents.models import (
    AgentThought,
    AgentTodo,
    AgentType,
    QueueEvent,
)
from internal.core.agent_v2.events.session import (
    EventTranslationSession,
    ToolCallRecord,
)
from internal.core.agent_v2.runtime.token_usage import normalize_message

MAIN_AGENT_ID = "main"
SUBAGENT_TOOL_NAME = "task"
TODO_TOOL_NAME = "write_todos"
SUBAGENT_TYPE_ARGUMENT = "subagent_type"


def response_messages(response: Any) -> list[BaseMessage]:
    """从模型响应包装中提取 LangChain 消息。"""
    model_response = getattr(response, "model_response", response)
    result = getattr(model_response, "result", model_response)
    if isinstance(result, BaseMessage):
        return [result]
    if isinstance(result, list):
        return [message for message in result if isinstance(message, BaseMessage)]
    return []


def translate_ai_message(
    session: EventTranslationSession,
    message: AIMessage,
) -> list[AgentThought]:
    """把模型消息翻译为有序业务事件。"""
    content = message_text(message.content)
    if not message.tool_calls:
        return [_message_thought(session, message, content)]

    thoughts: list[AgentThought] = []
    for raw_tool_call in message.tool_calls:
        tool_call = ToolCallRecord.from_mapping(raw_tool_call)
        session.remember_tool_call(tool_call)
        if tool_call.name == TODO_TOOL_NAME:
            continue
        thoughts.append(_tool_start_thought(session, message, content, tool_call))
    return thoughts


def translate_model_chunk(
    session: EventTranslationSession,
    message: AIMessage,
    metadata: Mapping[str, Any] | None = None,
) -> AgentThought | None:
    """把 messages 流中的文本增量翻译为一个业务事件。"""
    if not isinstance(message, AIMessage):
        return None
    content = message_text(message.content)
    usage = normalize_message(message)
    if not content and (
        getattr(message, "tool_calls", None)
        or getattr(message, "tool_call_chunks", None)
        or not usage.present
    ):
        return None

    stream_key = _stream_key(message, metadata)
    if content:
        session.mark_model_message_streamed(message, stream_key)
    return AgentThought(
        id=session.model_event_id(message, stream_key),
        task_id=session.task_id,
        event=QueueEvent.AGENT_MESSAGE,
        answer=content,
        message_token_count=usage.input_tokens,
        answer_token_count=usage.output_tokens,
        total_token_count=usage.total_tokens,
    )


def translate_tool_result(
    session: EventTranslationSession,
    result: ToolMessage | Command[Any],
    fallback: ToolCallRecord,
) -> list[AgentThought]:
    """翻译工具完成及原生 Todo 更新。"""
    messages = tool_messages(result)
    if not messages:
        return []

    thoughts: list[AgentThought] = []
    for message in messages:
        tool_call = session.resolve_tool_call(str(message.tool_call_id), fallback)
        if tool_call.name == TODO_TOOL_NAME:
            todos = command_todos(result)
            if todos is not None and session.remember_todo_call(tool_call.call_id):
                thoughts.append(_todo_update_thought(session, tool_call, todos))
            continue
        thoughts.append(_tool_end_thought(session, message, tool_call))
    return thoughts


def translate_graph_update(
    session: EventTranslationSession,
    update: Mapping[str, Any],
) -> list[AgentThought]:
    """从 0.6 updates 流补取原生 Todo 状态。"""
    thoughts: list[AgentThought] = []
    for value in update.values():
        if not isinstance(value, Mapping):
            continue
        raw_todos = value.get("todos")
        if not isinstance(raw_todos, list):
            continue
        todos = [AgentTodo.model_validate(todo) for todo in raw_todos]
        messages = value.get("messages", [])
        if not isinstance(messages, list):
            messages = [messages]
        tool_call_id = next(
            (
                str(message.tool_call_id)
                for message in messages
                if isinstance(message, ToolMessage) and message.tool_call_id
            ),
            "",
        )
        if not session.remember_todo_call(tool_call_id):
            continue
        tool_call = ToolCallRecord(
            call_id=tool_call_id,
            name=TODO_TOOL_NAME,
            arguments={"todos": [todo.model_dump(mode="json") for todo in todos]},
        )
        thoughts.append(_todo_update_thought(session, tool_call, todos))
    return thoughts


def tool_messages(result: ToolMessage | Command[Any]) -> list[ToolMessage]:
    """从直接结果或 Command 中提取工具消息。"""
    if isinstance(result, ToolMessage):
        return [result]
    if not isinstance(result, Command) or not isinstance(result.update, Mapping):
        return []
    messages = result.update.get("messages", [])
    if not isinstance(messages, list):
        messages = [messages]
    return [message for message in messages if isinstance(message, ToolMessage)]


def command_todos(result: ToolMessage | Command[Any]) -> list[AgentTodo] | None:
    """从成功 Command 更新读取权威 Todo 快照。"""
    if not isinstance(result, Command) or not isinstance(result.update, Mapping):
        return None
    raw_todos = result.update.get("todos")
    if not isinstance(raw_todos, list):
        return None
    return [AgentTodo.model_validate(todo) for todo in raw_todos]


def message_text(content: Any) -> str:
    """展开受支持的 LangChain 文本块。"""
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


def _message_thought(
    session: EventTranslationSession,
    message: AIMessage,
    content: str,
) -> AgentThought:
    """构造主 Agent 响应事件。"""
    return AgentThought(
        id=session.model_event_id(message),
        task_id=session.task_id,
        event=QueueEvent.AGENT_MESSAGE,
        answer=content,
        message_token_count=_usage_value(message, "input_tokens"),
        answer_token_count=_usage_value(message, "output_tokens"),
        total_token_count=_usage_value(message, "total_tokens"),
    )


def _tool_start_thought(
    session: EventTranslationSession,
    message: AIMessage,
    content: str,
    tool_call: ToolCallRecord,
) -> AgentThought:
    """构造普通动作或 SubAgent 委派事件。"""
    is_subagent = tool_call.name == SUBAGENT_TOOL_NAME
    display_name = (
        _subagent_name(tool_call)
        if is_subagent
        else tool_call.name or "工具"
    )
    return AgentThought(
        id=uuid4(),
        task_id=session.task_id,
        event=(QueueEvent.SUB_AGENT_DELEGATE if is_subagent else QueueEvent.AGENT_ACTION),
        thought=content or f"正在调用 {display_name}",
        tool=display_name,
        tool_input=tool_call.arguments,
        agent_id=MAIN_AGENT_ID,
        agent_type=AgentType.MAIN,
        delegation_id=tool_call.call_id or None if is_subagent else None,
        message_token_count=_usage_value(message, "input_tokens"),
        answer_token_count=_usage_value(message, "output_tokens"),
        total_token_count=_usage_value(message, "total_tokens"),
    )


def _tool_end_thought(
    session: EventTranslationSession,
    message: ToolMessage,
    tool_call: ToolCallRecord,
) -> AgentThought:
    """构造普通结果或 SubAgent 完成事件。"""
    is_subagent = tool_call.name == SUBAGENT_TOOL_NAME
    subagent_id = _subagent_name(tool_call) if is_subagent else None
    return AgentThought(
        id=uuid4(),
        task_id=session.task_id,
        event=QueueEvent.SUB_AGENT_END if is_subagent else QueueEvent.AGENT_ACTION,
        tool=subagent_id if is_subagent else tool_call.name,
        tool_input=tool_call.arguments,
        observation=message_text(message.content),
        agent_id=subagent_id if is_subagent else MAIN_AGENT_ID,
        agent_type=AgentType.SUBAGENT if is_subagent else AgentType.MAIN,
        parent_agent_id=MAIN_AGENT_ID if is_subagent else None,
        delegation_id=tool_call.call_id or None if is_subagent else None,
    )


def _subagent_name(tool_call: ToolCallRecord) -> str:
    """读取当前 DeepAgents task 工具的真实参数名。"""
    return str(
        tool_call.arguments.get(SUBAGENT_TYPE_ARGUMENT, SUBAGENT_TOOL_NAME)
    )


def _todo_update_thought(
    session: EventTranslationSession,
    tool_call: ToolCallRecord,
    todos: list[AgentTodo],
) -> AgentThought:
    """从 LangGraph 状态构造 Todo 快照事件。"""
    serialized_todos = [todo.model_dump(mode="json") for todo in todos]
    return AgentThought(
        id=uuid4(),
        task_id=session.task_id,
        event=QueueEvent.TODO_UPDATE,
        tool=TODO_TOOL_NAME,
        tool_input={"todos": serialized_todos},
        todos=todos,
    )


def _usage_value(message: AIMessage, key: str) -> int:
    """从 LangChain 用量元数据读取 Token 数。"""
    usage = normalize_message(message)
    return int(getattr(usage, key, 0) or 0)


def _stream_key(
    message: AIMessage,
    metadata: Mapping[str, Any] | None,
) -> str | None:
    """读取无消息 ID 时用于区分模型轮次的图元数据。"""
    if getattr(message, "id", None):
        return None
    if not metadata:
        return None
    return str(
        metadata.get("checkpoint_ns")
        or metadata.get("langgraph_checkpoint_ns")
        or metadata.get("langgraph_step")
        or ""
    ) or None


__all__ = [
    "command_todos",
    "message_text",
    "response_messages",
    "translate_graph_update",
    "tool_messages",
    "translate_ai_message",
    "translate_model_chunk",
    "translate_tool_result",
]
