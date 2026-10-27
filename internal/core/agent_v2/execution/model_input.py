"""本轮选项只随新输入进入模型历史，公开消息保持用户原文。"""

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

_INPUT_OPTIONS_KEY = "platform_input_options"


def build_model_input(message: BaseMessage, *, planning_enabled: bool) -> BaseMessage:
    """复制新用户消息并保留身份与媒体块；恢复执行不再次调用此装配。"""
    if not isinstance(message, HumanMessage):
        return message
    text_content = isinstance(message.content, str)
    content = [{"type": "text", "text": message.content}] if text_content else list(message.content)
    return message.model_copy(update={
        "content": [_planning_block(planning_enabled), *content],
        "additional_kwargs": {
            **message.additional_kwargs,
            _INPUT_OPTIONS_KEY: {
                "planning_enabled": planning_enabled,
                "text_content": text_content,
            },
        },
    })


def public_input_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """仅移除服务端标记且与首块一致的内部选项，不按正文标签猜测用户内容。"""
    extra = dict(message.get("additional_kwargs") or {})
    options = extra.pop(_INPUT_OPTIONS_KEY, None)
    content = message.get("content")
    if (
        (message.get("type") or message.get("role")) not in {"human", "user"}
        or not isinstance(options, Mapping)
        or type(options.get("planning_enabled")) is not bool
        or not isinstance(content, list)
        or not content
        or content[0] != _planning_block(options["planning_enabled"])
    ):
        return dict(message)
    original = content[1:]
    if (
        options.get("text_content")
        and len(original) == 1
        and isinstance(original[0], dict)
        and original[0].get("type") == "text"
    ):
        original = original[0]["text"]
    return {
        **message,
        "content": original,
        "additional_kwargs": extra,
    }


def _planning_block(enabled: bool) -> dict[str, str]:
    """只编码任务偏好，不包含身份、当前时间或随调用变化的观测字段。"""
    mode = "显式" if enabled else "自动"
    return {"type": "text", "text": f"<本轮选项>\n任务规划：{mode}\n</本轮选项>"}
