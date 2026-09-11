"""Agent 执行中的模型消息和文本解析。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from langchain_core.messages import BaseMessage


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


def current_turn_ai_contents(messages: Sequence[Any]) -> Iterator[Any]:
    """从后向前读取本轮 AI 内容，遇到用户消息停止，兼容原生消息和字典。"""
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
            yield content


def final_answer(messages: Sequence[Any]) -> str:
    """忽略本轮末尾的空工具调用消息，取最后一条有正文的 AI 回答。"""
    for content in current_turn_ai_contents(messages):
        text = message_content_to_text(content)
        if text:
            return text
    return ""


__all__ = ["current_turn_ai_contents", "final_answer", "message_content_to_text", "response_messages"]
