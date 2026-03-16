from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr, create_model

from internal.core.mcp.entities import MCPServerConfig, MCPToolCatalog


# JSON 类型映射表：将 JSON schema 类型映射到 Python 类型
JSON_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _build_args_schema(tool_catalog: MCPToolCatalog) -> type[BaseModel]:
    """
    构建工具参数模式
    
    将 MCP 工具的 JSON schema 转换为 Pydantic 模型，
    以便 LangChain 能够理解工具的参数结构
    
    Args:
        tool_catalog: MCP 工具目录信息
        
    Returns:
        动态生成的 Pydantic 模型类
    """
    schema = tool_catalog.input_schema or {}
    properties = schema.get("properties", {})
    required_fields = set(schema.get("required", []))
    fields = {}

    # 遍历所有属性，构建字段定义
    for field_name, field_schema in properties.items():
        # 映射 JSON 类型到 Python 类型
        field_type = JSON_TYPE_MAP.get(field_schema.get("type"), str)
        description = field_schema.get("description", "")
        
        # 根据是否必需设置不同的字段定义
        if field_name in required_fields:
            fields[field_name] = (field_type, Field(description=description))
        else:
            fields[field_name] = (
                field_type | None,
                Field(default=None, description=description),
            )

    # 动态创建 Pydantic 模型
    return create_model(f"MCPToolArgs_{tool_catalog.name}", **fields)


class MCPManagedTool(BaseTool):
    """
    MCP 托管工具类
    
    运行时管理的 MCP 工具，作为 LangChain BaseTool 暴露给上层应用
    这是一个代理类，本身不包含具体逻辑，而是转发给运行时管理器
    """

    _runtime_manager: Any = PrivateAttr()  # 运行时管理器实例
    _server_config: MCPServerConfig = PrivateAttr()  # 服务器配置
    _tool_name: str = PrivateAttr()  # 工具名称

    def __init__(
        self,
        *,
        runtime_manager: Any,
        server_config: MCPServerConfig,
        tool_catalog: MCPToolCatalog,
        **kwargs: Any,
    ):
        """
        初始化 MCP 托管工具
        
        Args:
            runtime_manager: 运行时管理器
            server_config: 服务器配置
            tool_catalog: 工具目录信息
            **kwargs: 其他参数
        """
        super().__init__(
            name=tool_catalog.name,
            description=tool_catalog.description,
            args_schema=_build_args_schema(tool_catalog),  # 动态构建参数模式
            **kwargs,
        )
        self._runtime_manager = runtime_manager
        self._server_config = server_config
        self._tool_name = tool_catalog.name

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        """
        执行工具方法
        
        拦截所有工具调用，转发给运行时管理器执行
        
        Args:
            *args: 位置参数
            **kwargs: 关键字参数（工具参数）
            
        Returns:
            工具执行结果
        """
        return self._runtime_manager.execute_tool(
            server_config=self._server_config,
            tool_name=self._tool_name,
            parameters=kwargs,
        )


def build_langchain_tool(
    runtime_manager: Any,
    server_config: MCPServerConfig,
    tool_catalog: MCPToolCatalog,
) -> MCPManagedTool:
    """
    构建 LangChain 工具
    
    工厂函数，用于创建 MCPManagedTool 实例
    
    Args:
        runtime_manager: 运行时管理器
        server_config: 服务器配置
        tool_catalog: 工具目录信息
        
    Returns:
        MCPManagedTool 实例
    """
    return MCPManagedTool(
        runtime_manager=runtime_manager,
        server_config=server_config,
        tool_catalog=tool_catalog,
    )