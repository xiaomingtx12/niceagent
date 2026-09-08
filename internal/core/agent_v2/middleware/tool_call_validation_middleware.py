"""在模型边界拒绝损坏的工具调用，避免执行猜测出来的参数。"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.messages import AIMessage, BaseMessage


class AgentMessageError(ValueError):
    """只携带消息位置和字段名，不把用户内容或工具参数写入异常。"""


def validate_tool_calls(messages: Sequence[BaseMessage]) -> None:
    """校验规范化调用和旧 checkpoint 中的原始调用，空参数对象合法。"""
    for index, message in enumerate(messages):
        if not isinstance(message, AIMessage):
            continue
        if message.invalid_tool_calls:
            raise AgentMessageError(f"第 {index + 1} 条消息包含无法解析的工具调用参数")
        calls = message.tool_calls
        if not calls:
            raw_calls = message.additional_kwargs.get("tool_calls", [])
            if not isinstance(raw_calls, list):
                raise AgentMessageError(f"第 {index + 1} 条消息的 tool_calls 不是数组")
            calls = []
            for raw in raw_calls:
                if not isinstance(raw, dict) or not isinstance(
                    raw.get("function"), dict
                ):
                    raise AgentMessageError(
                        f"第 {index + 1} 条消息缺少工具调用 function"
                    )
                function = raw["function"]
                try:
                    arguments = json.loads(function.get("arguments", ""))
                except (TypeError, ValueError) as exc:
                    raise AgentMessageError(
                        f"第 {index + 1} 条消息的工具调用 arguments 不是合法 JSON"
                    ) from exc
                calls.append(
                    {
                        "id": raw.get("id"),
                        "name": function.get("name"),
                        "args": arguments,
                    }
                )
        for call in calls:
            if not isinstance(call.get("name"), str) or not call["name"].strip():
                raise AgentMessageError(f"第 {index + 1} 条消息的工具调用 name 为空")
            if not isinstance(call.get("id"), str) or not call["id"].strip():
                raise AgentMessageError(f"第 {index + 1} 条消息的工具调用 id 为空")
            if not isinstance(call.get("args"), dict):
                raise AgentMessageError(
                    f"第 {index + 1} 条消息的工具调用 args 不是对象"
                )


class ToolCallValidationMiddleware(AgentMiddleware):
    """检查恢复的历史和新模型输出，不修改原消息或自动执行修复后的参数。"""

    def wrap_model_call(self, request: ModelRequest, handler: Callable):
        """在供应商序列化前定位异常字段。"""
        validate_tool_calls(request.messages)
        return handler(request)

    async def awrap_model_call(self, request: ModelRequest, handler: Callable):
        """异步调用保持同一消息约束。"""
        validate_tool_calls(request.messages)
        return await handler(request)

    def after_model(self, state: dict, runtime: Any) -> None:
        """不让无法执行的模型输出以成功回答结束 Run。"""
        validate_tool_calls(state.get("messages", [])[-1:])

    async def aafter_model(self, state: dict, runtime: Any) -> None:
        """异步图同样校验本次完整模型输出。"""
        self.after_model(state, runtime)
