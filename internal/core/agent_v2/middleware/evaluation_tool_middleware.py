"""测评图的工具访问限制，同时约束模型可见工具和实际调用。"""

from langchain.agents.middleware.types import AgentMiddleware


class EvaluationToolBoundary(AgentMiddleware):
    """执行测评只允许临时文件工具，模型不可委派到默认子 Agent 或执行命令。"""

    allowed = frozenset(
        {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "write_todos"}
    )

    def wrap_model_call(self, request, handler):
        """从模型可见工具中移除超出当前测评能力的工具。"""
        return handler(
            request.override(
                tools=[
                    tool
                    for tool in request.tools
                    if getattr(tool, "name", None) in self.allowed
                ]
            )
        )

    async def awrap_model_call(self, request, handler):
        """异步模型同样只暴露临时文件操作。"""
        return await handler(
            request.override(
                tools=[
                    tool
                    for tool in request.tools
                    if getattr(tool, "name", None) in self.allowed
                ]
            )
        )

    def wrap_tool_call(self, request, handler):
        """即使模型伪造隐藏工具调用，也不能突破临时环境。"""
        if request.tool_call["name"] not in self.allowed:
            raise ValueError("当前测评不允许调用此工具")
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        """异步工具执行沿用相同的白名单。"""
        if request.tool_call["name"] not in self.allowed:
            raise ValueError("当前测评不允许调用此工具")
        return await handler(request)
