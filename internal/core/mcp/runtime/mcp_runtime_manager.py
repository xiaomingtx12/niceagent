from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from injector import inject, singleton
from langchain_core.tools import BaseTool

from internal.core.mcp.adapters import build_langchain_tool
from internal.core.mcp.entities import MCPServerConfig, MCPToolCatalog, MCPTransport
from internal.core.mcp.security import MCPSecretCipher
from internal.exception import NotFoundException, ValidateErrorException
from internal.model import MCPServer


@inject
@singleton
@dataclass
class MCPRuntimeManager:
    """
    MCP 运行时管理器
    
    核心职责：在持久化的 MCP 配置和 LangChain 工具之间建立桥梁
    负责 MCP 连接管理、工具发现、工具执行和缓存管理
    
    设计亮点：
    - 单例模式确保全局唯一实例
    - 依赖注入支持松耦合
    - 进程内缓存提升性能
    """

    _tool_cache: dict[str, list[BaseTool]] = field(default_factory=dict, init=False)  # 工具缓存

    @classmethod
    def build_server_config(
        cls, server: MCPServer | MCPServerConfig
    ) -> MCPServerConfig:
        """
        构建服务器配置
        
        将数据库实体 MCPServer 转换为运行时配置 MCPServerConfig
        在此过程中会执行敏感信息的解密操作
        
        Args:
            server: 服务器实体或配置对象
            
        Returns:
            运行时服务器配置
        """
        if isinstance(server, MCPServerConfig):
            return server

        timeout_value = server.timeout if server.timeout else 30
        return MCPServerConfig(
            id=server.id,
            account_id=server.account_id,
            name=server.name,
            description=server.description,
            transport=MCPTransport(server.transport),
            enabled=server.enabled,
            url=server.url,
            command=server.command,
            args=server.args or [],
            env=MCPSecretCipher.decrypt_env(server.env or {}),  # 解密环境变量
            headers=MCPSecretCipher.decrypt_headers(server.headers or []),  # 解密请求头
            timeout=int(timeout_value),
            updated_at=server.updated_at,
        )

    @classmethod
    def _build_client_config(
        cls, server_config: MCPServerConfig
    ) -> dict[str, dict[str, Any]]:
        """
        构建客户端配置
        
        将服务配置转换为 langchain-mcp-adapters 需要的结构
        根据传输协议类型构建不同的配置格式
        
        Args:
            server_config: 服务器配置
            
        Returns:
            客户端配置字典
        """
        if server_config.transport == MCPTransport.STDIO:
            # STDIO 传输配置
            return {
                server_config.name: {
                    "transport": "stdio",
                    "command": server_config.command,
                    "args": server_config.args,
                    "env": server_config.env or {},
                }
            }

        # HTTP 传输配置
        headers = {
            header.get("key"): header.get("value")
            for header in server_config.headers
            if header.get("key")
        }
        http_config = {
            "transport": "http",
            "url": server_config.url,
        }
        if headers:
            http_config["headers"] = headers
        return {server_config.name: http_config}

    @classmethod
    def _cache_key(cls, server_config: MCPServerConfig) -> str:
        """
        生成缓存键
        
        使用服务器ID和更新时间生成唯一的缓存键
        当服务器配置更新时，缓存会自动失效
        
        Args:
            server_config: 服务器配置
            
        Returns:
            缓存键字符串
        """
        updated_at = server_config.updated_at or datetime.min
        return f"{server_config.id}:{updated_at.isoformat()}"

    async def _load_remote_tools_async(
        self, server_config: MCPServerConfig
    ) -> list[BaseTool]:
        """
        异步加载远程工具
        
        连接 MCP 服务器并发现可用的工具
        
        Args:
            server_config: 服务器配置
            
        Returns:
            远程工具列表
            
        Raises:
            ValidateErrorException: 如果未安装 langchain-mcp-adapters
        """
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError as exc:
            raise ValidateErrorException(
                "未安装 langchain-mcp-adapters，无法使用 MCP 功能"
            ) from exc

        # 创建 MCP 客户端并获取工具列表
        client = MultiServerMCPClient(self._build_client_config(server_config))
        return await client.get_tools()

    async def _execute_remote_tool_async(
        self,
        server_config: MCPServerConfig,
        tool_name: str,
        parameters: dict[str, Any],
    ) -> Any:
        tools = await self._load_remote_tools_async(server_config)
        for tool in tools:
            if tool.name == tool_name:
                return await tool.ainvoke(parameters)
        raise NotFoundException("该 MCP 工具不存在")

    @classmethod
    def _build_catalog_from_tool(cls, tool: BaseTool) -> MCPToolCatalog:
        input_schema = {}
        if getattr(tool, "args_schema", None) is not None:
            try:
                input_schema = tool.args_schema.model_json_schema()
            except Exception:
                input_schema = {}
        return MCPToolCatalog(
            name=tool.name,
            description=tool.description or "",
            input_schema=input_schema,
            raw_definition={
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": input_schema,
            },
        )

    def invalidate(self, server_id: str | None) -> None:
        if not server_id:
            return
        cache_keys = [
            cache_key
            for cache_key in self._tool_cache.keys()
            if cache_key.startswith(f"{server_id}:")
        ]
        for cache_key in cache_keys:
            self._tool_cache.pop(cache_key, None)

    def discover_tools(
        self, server: MCPServer | MCPServerConfig, force_refresh: bool = False
    ) -> list[MCPToolCatalog]:
        server_config = self.build_server_config(server)
        if not server_config.enabled:
            return []

        if force_refresh and server_config.id:
            self.invalidate(str(server_config.id))

        tools = asyncio.run(self._load_remote_tools_async(server_config))
        return [self._build_catalog_from_tool(tool) for tool in tools]

    def list_langchain_tools(
        self, server: MCPServer | MCPServerConfig, force_refresh: bool = False
    ) -> list[BaseTool]:
        server_config = self.build_server_config(server)
        cache_key = self._cache_key(server_config)

        if force_refresh and server_config.id:
            self.invalidate(str(server_config.id))

        if cache_key in self._tool_cache:
            return self._tool_cache[cache_key]

        tool_catalogs = self.discover_tools(server_config)
        tools = [
            build_langchain_tool(self, server_config, tool_catalog)
            for tool_catalog in tool_catalogs
        ]
        self._tool_cache[cache_key] = tools
        return tools

    def get_langchain_tool(
        self,
        server: MCPServer | MCPServerConfig,
        tool_name: str,
        force_refresh: bool = False,
    ) -> BaseTool:
        tools = self.list_langchain_tools(server, force_refresh=force_refresh)
        for tool in tools:
            if tool.name == tool_name:
                return tool
        raise NotFoundException("该 MCP 工具不存在")

    def execute_tool(
        self,
        *,
        server_config: MCPServerConfig,
        tool_name: str,
        parameters: dict[str, Any],
    ) -> Any:
        return asyncio.run(
            self._execute_remote_tool_async(server_config, tool_name, parameters)
        )