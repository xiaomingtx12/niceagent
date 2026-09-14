"""Agent 执行中的模型消息和文本解析。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from langchain_core.messages import BaseMessage


def json_value(value: Any) -> Any:
    """把框架对象转换为独立于供应商对象的 JSON 值。"""
    if isinstance(value, BaseMessage):
        try:
            return value.model_dump(mode="json")
        except (TypeError, ValueError):
            return {
                "type": value.type,
                "content": json_value(value.content),
                "id": value.id,
            }
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_value(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return json_value(model_dump(mode="json"))
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def response_messages(response: Any) -> list[BaseMessage]:
    """从 LangChain 模型响应包装中提取消息，不改变原响应对象。"""
    model_response = getattr(response, "model_response", response)
    result = getattr(model_response, "result", model_response)
    if isinstance(result, BaseMessage):
        return [result]
    if isinstance(result, list):
        return [message for message in result if isinstance(message, BaseMessage)]
    return []


def message_content_to_text(content: Any) -> str:
    """把受支持的消息文本块合并为纯文本，忽略非文本块。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping) and block.get("type") in {"text", None}:
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def final_answer(messages: Sequence[Any]) -> str:
    """忽略本轮末尾的空工具调用消息，取最后一条有正文的 AI 回答。"""
    for message in reversed(messages):
        if isinstance(message, BaseMessage):
            role, content = message.type, message.content
        elif isinstance(message, Mapping):
            role = message.get("type") or message.get("role")
            content = message.get("content")
        else:
            continue
        if role in {"human", "user"}:
            break
        if role in {"ai", "assistant"}:
            text = message_content_to_text(content)
            if text:
                return text
    return ""


__all__ = [
    "final_answer",
    "json_value",
    "message_content_to_text",
    "response_messages",
]
