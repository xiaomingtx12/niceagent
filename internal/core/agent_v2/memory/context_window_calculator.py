"""R0 deepagents 上下文窗口计算器。

deepagents 的 `SummarizationMiddleware` (R2 才正式接入) 触发压缩的方式：
- trigger = ("tokens", threshold) —— 当 messages 累计 token 数 > 模型 context_window * fraction 时触发
- 我们给上层一个纯函数 `should_trigger_summarization()`，R0 阶段用不到，只
  是给 R2 预留接口

R0 范围：只暴露
- `model_context_window(model_name) -> int` —— 查表兜底 128k
- `summarization_threshold(model_context_window) -> int` —— 给 SummarizationMiddleware
  的 trigger 阈值，默认 `model_context_window * 0.75`（deepagents 默认）
- `estimate_message_tokens(messages) -> int` —— 用 tokenizer 估算 token 数

约束（硬约束 #5：能用 langchain 的就用）：
- 不自造 token 计算，直接复用 `langchain_core.messages.convert_to_openai_messages`
  + `langchain_community.callbacks.get_openai_token_counter` 不可用（依赖 openai）
- 走"长度近似 + tiktoken fallback + char-based fallback"三档
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from langchain_core.messages import BaseMessage, get_buffer_string


# 模型 → context_window（token 数）兜底表
# 不依赖外部 SDK，纯查表；未知模型返回 _DEFAULT_CONTEXT_WINDOW
_DEFAULT_CONTEXT_WINDOW = 128_000

_MODEL_CONTEXT_WINDOW_HINTS: dict[str, int] = {
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o4-mini": 200_000,
    # Anthropic
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-opus-4-5": 200_000,
    # 国内
    "qwen-plus": 128_000,
    "qwen-max": 128_000,
    "qwen-turbo": 128_000,
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
    "glm-4": 128_000,
    "glm-4-plus": 128_000,
    "hunyuan-pro": 32_000,
}


@dataclass(frozen=True)
class TokenEstimate:
    """消息序列的 token 估算结果。

    `chars` 是字符数；`approx_tokens` 是按 `chars / 4` 估算（英文 1 token ≈ 4 chars，
    中文 1 token ≈ 1.5 chars，保守按 4 估）；`exact_tokens` 仅当 tiktoken 可用
    且明确指定 `model_name` 时返回。
    """

    chars: int
    approx_tokens: int
    exact_tokens: Optional[int] = None

    @property
    def tokens(self) -> int:
        """优先 exact，否则 approx。"""
        return self.exact_tokens if self.exact_tokens is not None else self.approx_tokens


def model_context_window(model_name: Optional[str]) -> int:
    """查模型 context_window（token 数），未知模型返回兜底 128k。

    `model_name` 通常形如 `provider/model`（langchain init_chat_model 风格）
    或 `gpt-4o` 直接是模型名。我们按 `/` 切分取最后一段，再做前缀匹配。
    """
    if not model_name:
        return _DEFAULT_CONTEXT_WINDOW
    bare = model_name.split("/")[-1].lower()
    if bare in _MODEL_CONTEXT_WINDOW_HINTS:
        return _MODEL_CONTEXT_WINDOW_HINTS[bare]
    # 前缀匹配：claude-sonnet-4-5-20251022 → claude-sonnet-4-5
    for key in sorted(_MODEL_CONTEXT_WINDOW_HINTS.keys(), key=len, reverse=True):
        if bare.startswith(key):
            return _MODEL_CONTEXT_WINDOW_HINTS[key]
    return _DEFAULT_CONTEXT_WINDOW


def summarization_threshold(model_context_window: int, *, fraction: float = 0.75) -> int:
    """返回 `SummarizationMiddleware(trigger=("tokens", threshold))` 的阈值。

    deepagents 默认 fraction=0.75；保留入口让 R2 配置中心可以改。
    """
    return int(model_context_window * fraction)


def estimate_message_tokens(
    messages: Iterable[BaseMessage],
    *,
    model_name: Optional[str] = None,
) -> TokenEstimate:
    """估算消息序列 token 数。

    - 总是返回 chars + approx_tokens（≈ chars/4 向下取整）
    - 尝试用 tiktoken 算 exact（仅当指定 model_name + tiktoken 可用）
    """
    messages_list = list(messages)
    if not messages_list:
        return TokenEstimate(chars=0, approx_tokens=0, exact_tokens=0)

    # 用 langchain_core 的 get_buffer_string 把消息拼成字符串（带 Human/AI/Tool 前缀）
    buf = get_buffer_string(messages_list)
    chars = len(buf)
    approx_tokens = chars // 4

    exact = _try_tiktoken_count(buf, model_name)
    return TokenEstimate(chars=chars, approx_tokens=approx_tokens, exact_tokens=exact)


def should_trigger_summarization(
    current_tokens: int,
    model_context_window: int,
    *,
    fraction: float = 0.75,
) -> bool:
    """R0 阶段直接暴露 `current_tokens >= model_context_window * fraction` 判断。

    R2 接入 SummarizationMiddleware 后，这一函数会被替代；保留是为了单测可覆盖。
    """
    return current_tokens >= summarization_threshold(model_context_window, fraction=fraction)


def _try_tiktoken_count(buf: str, model_name: Optional[str]) -> Optional[int]:
    """tiktoken 可用且 model_name 有对应 encoding 时返回精确 token 数。"""
    try:
        import tiktoken  # type: ignore
    except ImportError:
        return None
    if not model_name:
        return None
    try:
        enc = tiktoken.encoding_for_model(model_name.split("/")[-1])
        return len(enc.encode(buf))
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "TokenEstimate",
    "estimate_message_tokens",
    "model_context_window",
    "should_trigger_summarization",
    "summarization_threshold",
]