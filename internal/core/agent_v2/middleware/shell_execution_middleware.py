"""集中处理共享 Shell 的能力限制及其与文件工具不同的路径语义。"""

from collections.abc import Callable

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ToolCallRequest
from langchain_core.messages import ToolMessage


_VIRTUAL_PATH_INSTRUCTIONS = """执行环境路径说明：
文件工具继续使用 /workspace/、/skills/、/subagent_skills/ 等虚拟路径。
execute 每次从这些目录的共同物理父目录启动；Shell 命令请使用 workspace/、skills/、subagent_skills/ 相对路径。
例如文件工具的 /workspace/run.py 对应命令 python workspace/run.py。若程序需要绝对路径，先通过 pwd（Windows 使用 cd）获取真实目录。
命令和程序中的字符串会原样执行，不会自动替换虚拟路径。"""


class ShellExecutionMiddleware(AgentMiddleware):
    """文件权限不约束 Shell；在模型与执行入口同时限制子 Agent 的共享执行能力。"""

    def __init__(self, *, allow_execution: bool = True, virtual_paths: bool = False) -> None:
        """策略来自装配配置，不读取模型提供的授权或路径开关。"""
        self._allow_execution = allow_execution
        self._virtual_paths = virtual_paths

    def wrap_model_call(self, request: ModelRequest, handler: Callable):
        """同步模型仅看到可用工具和适用的执行路径说明。"""
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
        """只调整当前请求，避免修改共享工具实例或重复累积提示词。"""
        # 1.子 Agent 不暴露共享 Shell；文件工具仍由框架检查路径权限。
        if not self._allow_execution:
            return request.override(tools=[
                tool for tool in request.tools
                if (tool.get("name") if isinstance(tool, dict) else tool.name) != "execute"
            ])
        # 2.仅虚拟文件系统需要补充物理执行路径，原生 Sandbox 保持原有说明。
        if self._virtual_paths and any(
            (tool.get("name") if isinstance(tool, dict) else tool.name) == "execute"
            for tool in request.tools
        ):
            return request.override(system_message=append_to_system_message(
                request.system_message, _VIRTUAL_PATH_INSTRUCTIONS,
            ))
        return request

    def _denied_call(self, request: ToolCallRequest) -> ToolMessage | None:
        """文件权限无法约束任意命令，拒绝整个能力而不尝试解析 Shell 授权。"""
        if not self._allow_execution and request.tool_call["name"] == "execute":
            return ToolMessage(
                content="当前子 Agent 不允许执行共享 Shell，请使用已授权文件工具；需要运行命令时交由主 Agent 处理。",
                tool_call_id=request.tool_call["id"],
                name="execute",
                status="error",
            )
        return None
