"""模型调用时追加的运行时上下文。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest

from internal.core.agent_v2.assembly.configuration.agent_system_prompt import (
    build_dynamic_context,
)


class RuntimeContextMiddleware(AgentMiddleware):
    """为模型调用追加当前运行时间。"""

    @staticmethod
    def _dynamic_context() -> str:
        """构造当前模型调用的运行时间上下文。"""
        return build_dynamic_context(
            invoked_at=datetime.now(timezone.utc).isoformat(),
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
                    self._dynamic_context(),
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
                    self._dynamic_context(),
                )
            )
        )


__all__ = ["RuntimeContextMiddleware"]
