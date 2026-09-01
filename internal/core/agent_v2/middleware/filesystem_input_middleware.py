"""Deep Agents 文件工具参数的兼容性修正。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command


class FilesystemInputMiddleware(AgentMiddleware):
    """为省略目录参数的文件工具调用补齐安全默认值。"""

    @staticmethod
    def _normalize(request: ToolCallRequest) -> ToolCallRequest:
        """确保 ``ls`` 始终带有虚拟根目录参数。"""
        call = request.tool_call
        if call.get("name") != "ls":
            return request
        args = call.get("args")
        normalized_args = dict(args) if isinstance(args, Mapping) else {}
        if normalized_args.get("path"):
            return request
        normalized_args["path"] = "/"
        return request.override(tool_call={**call, "args": normalized_args})

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """在文件工具 Schema 校验前补齐 ``ls.path``。"""
        return handler(self._normalize(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """异步版本的 ``ls.path`` 兼容处理。"""
        return await handler(self._normalize(request))


__all__ = ["FilesystemInputMiddleware"]
