"""从消息中提取正文：读取内容块文本，或从本轮对话中选取最终 AI 回答。"""

from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import BaseMessage


def extract_final_answer(messages: Sequence[Any]) -> str:
    """倒序取本轮最后一条有正文的 AI 消息；遇到用户消息即停止，避免取到上一轮回答。"""
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
            text = extract_text(content)
            if text:
                return text
    return ""


def extract_text(content: Any) -> str:
    """只拼接字符串和文本内容块，图片、推理等非正文块不进入回答或文本估算。"""
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


__all__ = ["extract_final_answer", "extract_text"]
