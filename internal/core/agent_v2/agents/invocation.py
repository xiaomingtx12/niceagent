"""Agent V2 调用的纯输入预处理。"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from internal.exception import FailException
def extract_query_and_images(input_state: dict[str, Any]) -> tuple[str, list[str]]:
    """提取最新用户问题和图片地址。"""
    # 1.读取本次调用携带的消息列表，并拒绝空输入。
    messages = list(input_state.get("messages", []) or [])
    if not messages:
        raise FailException("智能体输入消息不能为空")
    # 2.优先选择最后一条 HumanMessage，兼容其他消息类型作为兜底。
    current_message = next(
        (
            message
            for message in reversed(messages)
            if isinstance(message, HumanMessage)
        ),
        messages[-1],
    )
    content = getattr(current_message, "content", "")
    # 3.拆分文本块和图片块，分别保留给模型和业务结果使用。
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return str(content or ""), []

    query_parts: list[str] = []
    image_urls: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            query_parts.append(str(block.get("text", "")))
        elif block.get("type") == "image":
            if block.get("url"):
                image_urls.append(str(block["url"]))
        elif block.get("type") == "image_url":
            image = block.get("image_url", {})
            if isinstance(image, dict) and image.get("url"):
                image_urls.append(str(image["url"]))
    # 4.合并文本并返回图片地址列表。
    return "".join(query_parts), image_urls


__all__ = ["extract_query_and_images"]
