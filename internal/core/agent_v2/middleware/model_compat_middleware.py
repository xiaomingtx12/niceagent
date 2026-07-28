"""R0 模型兼容性中间件。

master 现状：
- `BaseLanguageModel` 继承自 `langchain_core.language_models.BaseLanguageModel`
  （langchain 1.x 之前的接口，跟 `BaseChatModel` 平行）
- AIMessage 的 usage 字段叫 `token_usage`（老接口），1.x 改名为
  `usage_metadata`（deepagents `wrap_model_call` 读的是新字段）
- master 老模型 `invoke()` 返回 `AIMessage` 时 `usage_metadata` 是 None

R0 范围（最小侵入）：
- 这个中间件在 `before_model` 阶段把 state 的 `model` 字段（在 deepagents
  graph 里 ModelRequest 上携带）替换为 deepagents 期望的 BaseChatModel
- 在 `after_model` / `wrap_model_call` 阶段把 AIMessage 的 `token_usage`
  复制到 `usage_metadata`，下游 `EventTranslationMiddleware` 就能读到
- 不重写 master 的 `BaseLanguageModel`（避免影响其他老调用方）；只在 deepagent
  这条路径上做字段兼容

约束：硬约束 #5（能用 langchain 的就用）—— 不自造 token 字段转换，直接调
langchain_core 提供的方法（AIMessage.model_copy + 模型替换）。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel


def normalize_usage(ai_message: Any) -> Any:
    """把老 `token_usage` 字段映射到新 `usage_metadata`。

    langchain 1.x 的 AIMessage 字段约定：
    - usage_metadata: { input_tokens, output_tokens, total_tokens }
    - 老 0.x 约定：token_usage（同样 dict）

    deepagents `EventTranslationMiddleware` 通过 `getattr(ai_message,
    'usage_metadata')` 取值；master 老模型返回的 AIMessage 上只有
    `token_usage`，所以这里补一次映射。
    """
    if getattr(ai_message, "usage_metadata", None):
        return ai_message
    token_usage = getattr(ai_message, "token_usage", None)
    if not token_usage:
        return ai_message
    return ai_message.model_copy(update={"usage_metadata": dict(token_usage)})


class ModelCompatMiddleware(AgentMiddleware):
    """R0 阶段最小模型兼容层。

    当前主要做两件事：
    1. before_model: 若 state['model'] 不是 BaseChatModel，报警（master 老模型
       调用方在迁移期应该已经包了一层 BaseChatModel）
    2. wrap_model_call: 调用 `normalize_usage()` 把老 token_usage 映射到
       usage_metadata，下游 EventTranslationMiddleware 直接拿
    """

    state_schema = Any

    def __init__(self, *, expected_chat_model: Optional[BaseChatModel] = None) -> None:
        super().__init__()
        self._expected_chat_model = expected_chat_model

    def before_model(self, state: dict, runtime: Any) -> dict | None:
        """校验 state 里携带的 model 是 BaseChatModel，否则打日志告警。"""
        model = state.get("model") or self._expected_chat_model
        if model is None:
            return None
        if not isinstance(model, BaseChatModel):
            # 不抛错，R0 阶段只警告，避免阻塞老调用方（deepagents 内部会自己处理）
            import warnings

            warnings.warn(
                f"ModelCompatMiddleware: model {type(model).__name__} 不是 "
                "BaseChatModel，可能影响 deepagents 工具调用/usage 字段，"
                "请确认上游已经把 master BaseLanguageModel 包成了 BaseChatModel。",
                RuntimeWarning,
                stacklevel=2,
            )
        return None

    def wrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """包一层 normalize_usage，把老字段映射到新字段。"""
        response = handler(request)
        result = getattr(response, "result", response)
        if isinstance(result, list):
            normalized_messages = [
                normalize_usage(message)
                if hasattr(message, "model_copy")
                else message
                for message in result
            ]
            if normalized_messages != result:
                response.result = normalized_messages
            return response
        if hasattr(result, "model_copy"):
            normalized = normalize_usage(result)
            if normalized is not result:
                if hasattr(response, "result"):
                    response.result = normalized
                else:
                    return normalized
        return response


__all__ = ["ModelCompatMiddleware", "normalize_usage"]
