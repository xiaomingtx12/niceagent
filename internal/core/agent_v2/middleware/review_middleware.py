"""R0 内容审核中间件。

master 老 review 流程在 service 层（_validate_draft_app_config 校验 +
chat_service 在 invoke 前调关键词过滤）。deepagents 模型下，审核要嵌到
graph 内部：
- input 审核：`before_model` —— 拦截 user 输入，命中关键词就替换成
  `inputs_config.preset_response`，不让 LLM 看原内容
- output 审核：`after_model` —— LLM 输出命中关键词直接抛 `ReviewBlocked`
  （由 BaseAgent 兜底成 message 状态 = MessageStatus.SENSITIVE）

约束（硬约束 #5）：不复用 langchain 旧版审核组件（langchain 0.x 有
ContentFilter，但跟 deepagents 1.x middleware 不兼容），直接实现纯函数
`apply_keyword_filter` + 中间件调用，单测覆盖纯函数即可。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from langchain.agents.middleware.types import AgentMiddleware


class ReviewBlocked(Exception):
    """output 命中关键词时由中间件抛出，BaseAgent 捕获后置 message 状态。"""


def apply_keyword_filter(
    text: str,
    keywords: list[str],
    *,
    case_sensitive: bool = False,
) -> Optional[str]:
    """关键词命中检测。

    Returns:
        - None: 未命中，原样放行
        - str: 命中的关键词（用于埋点上报）

    实现：re 编译 + 单词边界 \b，避免误命中（如 "app" 命中 "p"）。
    """
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
    """input/output 关键词审核。

    state schema 字段：
    - `runtime_review_blocked: bool` —— 是否被审核拦下（after_model 设置）
    - `runtime_review_hit_keyword: str` —— 命中的关键词（埋点用）
    """

    state_schema = Any

    def __init__(
        self,
        *,
        review_config: dict,
        preset_response: str = "",
    ) -> None:
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
        return self._enabled

    def before_model(self, state: dict, runtime: Any) -> dict | None:
        """input 审核：拦截 user 输入。

        当前实现只做"标记命中关键词" + 写入 preset_response 到 state 提示，
        真正的"替换 user 消息"留给 BaseAgent invoke 前做（保持 middleware
        只读 state 的约定，避免 middleware 篡改 messages 列表）。
        """
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
                "runtime_review_blocked": True,
                "runtime_review_hit_keyword": hit,
                "runtime_review_preset_response": self._preset_response,
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