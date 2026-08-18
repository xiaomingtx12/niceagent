"""处理模型输入和输出的关键词审核。"""

from __future__ import annotations

import re
from typing import Any, Optional

from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage


class ReviewBlocked(Exception):
    """output 命中关键词时由中间件抛出，BaseAgent 捕获后置 message 状态。"""


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

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: dict, runtime: Any) -> dict | None:
        """命中输入关键词时返回预设响应并结束图。"""
        if not self._enabled or not self._inputs_enabled or not self._keywords:
            return None
        messages = state.get("messages", [])
        if not messages:
            return None
        last = messages[-1]
        text = getattr(last, "content", "") or ""
        if not isinstance(text, str):
            return None
        hit = apply_keyword_filter(text, self._keywords)
        if hit is not None:
            return {
                "jump_to": "end",
                "messages": [
                    AIMessage(
                        content=self._preset_response or "输入内容未通过审核。"
                    )
                ],
            }
        return None

    def after_model(self, state: dict, runtime: Any) -> dict | None:
        """output 审核：拦截 LLM 回答。"""
        if not self._enabled or not self._outputs_enabled or not self._keywords:
            return None
        messages = state.get("messages", [])
        if not messages:
            return None
        last = messages[-1]
        text = getattr(last, "content", "") or ""
        if not isinstance(text, str):
            return None
        hit = apply_keyword_filter(text, self._keywords)
        if hit is not None:
            raise ReviewBlocked(
                f"LLM 输出命中关键词 '{hit}'，已拦截。"
            )
        return None


__all__ = ["ReviewBlocked", "ReviewMiddleware", "apply_keyword_filter"]
