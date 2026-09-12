"""平台中间件的启用条件、固定顺序和运行策略。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ModelRetryMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langchain.agents.middleware.types import AgentMiddleware
from openai import APIConnectionError

from internal.core.agent_v2.execution.runtime_resources import AgentRuntimeResources
from internal.core.agent_v2.middleware.reloading_memory_middleware import ReloadingMemoryMiddleware
from internal.core.agent_v2.middleware.context_window_middleware import ContextWindowMiddleware
from internal.core.agent_v2.middleware.review_middleware import ReviewMiddleware
from internal.core.agent_v2.middleware.reloading_skills_middleware import ReloadingSkillsMiddleware
from internal.core.agent_v2.middleware.runtime_context_middleware import (
    RuntimeContextMiddleware,
)
from internal.core.agent_v2.middleware.tool_call_validation_middleware import (
    ToolCallValidationMiddleware,
)
from internal.core.agent_v2.middleware.user_input_middleware import UserInputMiddleware

_RETRYABLE_MODEL_STATUS_CODES = frozenset({408, 409, 429})
_RETRYABLE_READ_ONLY_TOOLS = ("ls", "read_file", "glob", "grep")
_MODEL_CALL_LIMIT = 10
_TOOL_CALL_LIMIT = 40


def build_platform_middleware(
    *,
    review_config: Mapping[str, Any],
    runtime: AgentRuntimeResources,
) -> list[AgentMiddleware]:
    """按稳定顺序构造 MyDify 扩展的 DeepAgents 中间件。"""
    # 顺序影响 wrap 嵌套与 before/after 钩子，按职责注释但不重排。
    middleware: list[AgentMiddleware] = [
        # 基础设施：刷新授权 Skill，再校验消息并注入本轮上下文。
        ReloadingSkillsMiddleware(
            backend=runtime.backend,
            sources=list(runtime.skill_sources),
            system_prompt=None,
        ),
        ToolCallValidationMiddleware(),
        RuntimeContextMiddleware(),
        # 业务约束：保护图状态；公开事件审核由执行阶段单独负责。
        ReviewMiddleware(review_config=dict(review_config)),
        # 运行策略：保留模型重试、只读工具重试与调用限制的原有顺序。
        ModelRetryMiddleware(
            max_retries=2,
            retry_on=_is_retryable_model_error,
            on_failure="error",
        ),
        # 只有无副作用的内置读取工具可以自动重试；写入和未知业务工具
        # 无法仅凭异常确认前一次调用是否已经生效。
        ToolRetryMiddleware(
            max_retries=2,
            tools=list(_RETRYABLE_READ_ONLY_TOOLS),
            retry_on=(TimeoutError, ConnectionError),
            on_failure="error",
        ),
        ModelCallLimitMiddleware(
            run_limit=_MODEL_CALL_LIMIT,
            exit_behavior="end",
        ),
        ToolCallLimitMiddleware(
            run_limit=_TOOL_CALL_LIMIT,
            exit_behavior="continue",
        ),
    ]
    # 记忆先刷新，窗口观测才能看到实际注入的提示词。
    if runtime.memory_sources:
        middleware.append(ReloadingMemoryMiddleware(backend=runtime.backend, sources=list(runtime.memory_sources)))
    middleware.append(ContextWindowMiddleware())
    if runtime.runtime_context.user_input_enabled and runtime.checkpointer:
        middleware.append(UserInputMiddleware())
    return middleware


def _is_retryable_model_error(exc: Exception) -> bool:
    """仅重试当前 OpenAI 兼容模型链路中明确的瞬时故障。"""
    if isinstance(exc, (TimeoutError, ConnectionError, APIConnectionError)):
        return True

    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code in _RETRYABLE_MODEL_STATUS_CODES or 500 <= status_code <= 599
    return False


__all__ = ["build_platform_middleware"]
