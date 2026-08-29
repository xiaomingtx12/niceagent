"""LangChain 消息包装和内容的统一解析。"""

from __future__ import annotations

from collections.abc import Mapping
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


__all__ = ["message_content_to_text", "response_messages"]
