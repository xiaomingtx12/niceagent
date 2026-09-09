"""模型调用时追加的运行时上下文。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest

from internal.core.agent_v2.assembly.configuration.agent_system_prompt import (
    build_dynamic_context,
)
from internal.core.agent_v2.entity.run_context import AgentRunContext


class RuntimeContextMiddleware(AgentMiddleware):
    """为模型调用追加当前运行时间和本轮规划偏好。"""

    @staticmethod
    def _dynamic_context(request: ModelRequest) -> str:
        """只读取可信的本轮上下文，未开启时保留原生自动规划行为。"""
        context = request.runtime.context if request.runtime else None
        return build_dynamic_context(
            invoked_at=datetime.now(timezone.utc).isoformat(),
            planning_enabled=isinstance(context, AgentRunContext) and context.planning_enabled,
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable,
    ):
        """为同步模型调用追加运行时间。"""
        return handler(
            request.override(
                system_message=append_to_system_message(
                    request.system_message,
                    self._dynamic_context(request),
                )
            )
        )

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable,
    ):
        """为异步模型调用追加运行时间。"""
        return await handler(
            request.override(
                system_message=append_to_system_message(
                    request.system_message,
                    self._dynamic_context(request),
                )
            )
        )


__all__ = ["RuntimeContextMiddleware"]
