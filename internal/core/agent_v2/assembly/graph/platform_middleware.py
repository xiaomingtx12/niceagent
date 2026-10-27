"""平台中间件的启用条件、固定顺序和运行策略。"""

from __future__ import annotations

from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ModelRetryMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langchain.agents.middleware.types import AgentMiddleware
from openai import APIConnectionError

from internal.core.agent_v2.execution.runtime_resources import AgentRuntimeResources
from internal.core.agent_v2.middleware.context_window_middleware import (
    ContextWindowMiddleware,
)
from internal.core.agent_v2.middleware.agent_memory_middleware import (
    AgentMemoryMiddleware,
)
from internal.core.agent_v2.middleware.reloading_skills_middleware import (
    ReloadingSkillsMiddleware,
)
from internal.core.agent_v2.middleware.tool_call_validation_middleware import (
    ToolCallValidationMiddleware,
)
from internal.core.agent_v2.middleware.user_input_middleware import UserInputMiddleware

_RETRYABLE_MODEL_STATUS_CODES = frozenset({408, 409, 429})
_RETRYABLE_READ_ONLY_TOOLS = ("ls", "read_file", "glob", "grep")
_MODEL_CALL_LIMIT = 50
_TOOL_CALL_LIMIT = 200


def build_platform_middleware(
    *,
    runtime: AgentRuntimeResources,
) -> list[AgentMiddleware]:
    """按稳定顺序构造 MyDify 扩展的 DeepAgents 中间件。"""
    # 1.基础设施层：刷新授权 Skill，再校验工具消息。
    middleware: list[AgentMiddleware] = [
        ReloadingSkillsMiddleware(
            backend=runtime.backend,
            sources=list(runtime.skill_projection.sources),
            system_prompt=None,
        ),
        ToolCallValidationMiddleware(),
        # 2.运行策略层：模型重试、只读工具重试和调用限制保持固定顺序。
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
    # 3.记忆工具与上下文观测；固定记忆规则已在构图时加入 system。
    if runtime.runtime_context.memory_enabled:
        middleware.append(AgentMemoryMiddleware())
    middleware.append(ContextWindowMiddleware())
    # 4.交互能力层：只有启用持久化时才允许恢复用户输入状态。
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
