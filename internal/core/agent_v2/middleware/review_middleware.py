"""处理模型输入和输出的关键词审核。"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Optional

from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage

from internal.core.agent_v2.execution.message_parser import (
    message_content_to_text,
    response_messages,
)


class ReviewBlocked(Exception):
    """output 命中关键词时由中间件抛出，由执行边界统一处理。"""


def apply_keyword_filter(
    text: str,
    keywords: list[str],
    *,
    case_sensitive: bool = False,
) -> Optional[str]:
    """返回首个命中的关键词，未命中时返回 None。"""
    if not text or not keywords:
        return None
    flags = 0 if case_sensitive else re.IGNORECASE
    for kw in keywords:
        if not kw:
            continue
        # \b 在中文场景下是空格/标点边界，按字符 word boundary
        pattern = r"(?<![A-Za-z0-9_])" + re.escape(kw) + r"(?![A-Za-z0-9_])"
        if re.search(pattern, text, flags):
            return kw
    return None


class ReviewMiddleware(AgentMiddleware):
    """执行输入和输出关键词审核。"""

    def __init__(
        self,
        *,
        review_config: dict,
        preset_response: str = "",
    ) -> None:
        """编译输入和输出审核策略。"""
        super().__init__()
        cfg = review_config or {}
        self._enabled: bool = bool(cfg.get("enable", False))
        self._keywords: list[str] = list(cfg.get("keywords", []) or [])
        inputs = (cfg.get("inputs_config") or {}) if self._enabled else {}
        outputs = (cfg.get("outputs_config") or {}) if self._enabled else {}
        self._inputs_enabled: bool = bool(inputs.get("enable", False))
        self._outputs_enabled: bool = bool(outputs.get("enable", False))
        self._preset_response: str = (
            inputs.get("preset_response", preset_response) or preset_response
        )

    @property
    def enabled(self) -> bool:
        """返回是否启用任一审核策略。"""
        return self._enabled

    @property
    def output_enabled(self) -> bool:
        """仅在有实际输出规则时延迟公开事件。"""
        return self._enabled and self._outputs_enabled and bool(self._keywords)

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: dict, runtime: Any) -> dict | None:
        """命中输入关键词时返回预设响应并结束图。"""
        if not self._enabled or not self._inputs_enabled or not self._keywords:
            return None
        messages = state.get("messages", [])
        if not messages:
            return None
        last = messages[-1]
        role = (last.get("type") or last.get("role")) if isinstance(last, Mapping) else getattr(last, "type", "")
        if role not in {"human", "user"}:
            return None
        content = last.get("content", "") if isinstance(last, Mapping) else getattr(last, "content", "")
        text = message_content_to_text(content)
        hit = apply_keyword_filter(text, self._keywords)
        if hit is not None:
            return {
                "jump_to": "end",
                "messages": [
                    AIMessage(content=self._preset_response or "输入内容未通过审核。")
                ],
            }
        return None

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: dict, runtime: Any) -> dict | None:
        """异步图沿用相同的用户输入文本审核。"""
        return self.before_model(state, runtime)

    def wrap_model_call(self, request: Any, handler: Callable) -> Any:
        """在回答写入图状态前审核，避免失败回答进入后续轮次上下文。"""
        response = handler(request)
        for message in response_messages(response):
            self.check_output(message.content)
        return response

    async def awrap_model_call(self, request: Any, handler: Callable[..., Awaitable]) -> Any:
        """异步调用与同步调用遵守相同的状态写入边界。"""
        response = await handler(request)
        for message in response_messages(response):
            self.check_output(message.content)
        return response

    def check_output(self, content: Any) -> None:
        """供模型和事件交付边界共用文本块审核规则。"""
        if self.output_enabled and apply_keyword_filter(
            message_content_to_text(content), self._keywords
        ) is not None:
            raise ReviewBlocked("输出内容未通过审核。")


__all__ = ["ReviewBlocked", "ReviewMiddleware", "apply_keyword_filter"]
