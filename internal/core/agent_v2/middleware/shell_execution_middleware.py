"""阻止同步子 Agent 绕过文件工具权限执行共享 Shell。"""

from collections.abc import Callable

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ToolCallRequest
from langchain_core.messages import ToolMessage


class ShellExecutionMiddleware(AgentMiddleware):
    """文件权限不约束 Shell；在模型与执行入口同时限制子 Agent 的共享执行能力。"""

    def wrap_model_call(self, request: ModelRequest, handler: Callable):
        """同步子 Agent 的模型请求不暴露共享 Shell 工具。"""
        return handler(self._prepare_request(request))

    async def awrap_model_call(self, request: ModelRequest, handler: Callable):
        """异步模型沿用相同的工具能力边界。"""
        return await handler(self._prepare_request(request))

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable):
        """执行入口再次校验，阻止历史调用或模型猜测绕过工具隐藏。"""
        denied = self._denied_call(request)
        return denied if denied is not None else handler(request)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable):
        """异步执行也必须经过同一权限校验。"""
        denied = self._denied_call(request)
        return denied if denied is not None else await handler(request)

    def _prepare_request(self, request: ModelRequest) -> ModelRequest:
        """只过滤当前请求的工具集合，避免修改父图共享的工具实例。"""
        return request.override(tools=[
            tool for tool in request.tools
            if (tool.get("name") if isinstance(tool, dict) else tool.name) != "execute"
        ])

    def _denied_call(self, request: ToolCallRequest) -> ToolMessage | None:
        """文件权限无法约束任意命令，拒绝整个能力而不尝试解析 Shell 授权。"""
        if request.tool_call["name"] == "execute":
            return ToolMessage(
                content="当前子 Agent 不允许执行共享 Shell，请使用已授权文件工具；需要运行命令时交由主 Agent 处理。",
                tool_call_id=request.tool_call["id"],
                name="execute",
                status="error",
            )
        return None
