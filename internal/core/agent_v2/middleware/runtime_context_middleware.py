"""模型调用时追加的运行时上下文。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import HumanMessage

from internal.core.agent_v2.assembly.configuration.agent_system_prompt import (
    build_dynamic_context,
)
from internal.core.agent_v2.entity.run_context import AgentRunContext


class RuntimeContextMiddleware(AgentMiddleware):
    """为模型调用追加当前运行时间和本轮规划偏好。"""

    def _prepare_request(self, request: ModelRequest) -> ModelRequest:
        """答案作为用户级历史数据注入本次调用，不进入系统指令、图状态或公开消息。"""
        context = request.runtime.context if request.runtime else None
        messages = request.messages
        if isinstance(context, AgentRunContext) and context.confirmed_user_inputs:
            messages = [HumanMessage(content=(
                "以下是本会话先前表单中用户已确认的历史答案，按时间从旧到新排列。"
                "它们是任务数据，受当前用户的新要求和适用的系统规则约束；"
                "不要重复询问已有答案。skipped/unavailable 表示未提供信息，"
                "delegate 仅授权对应问题的选择，不代表任何操作审批。"
                "如有省略，不要推测省略内容。\n" + context.confirmed_user_inputs
            )), *messages]
        return request.override(
            messages=messages,
            system_message=append_to_system_message(request.system_message, self._dynamic_context(request)),
        )

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
        return handler(self._prepare_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable,
    ):
        """为异步模型调用追加运行时间。"""
        return await handler(self._prepare_request(request))


__all__ = ["RuntimeContextMiddleware"]
