from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class MCPTransport(str, Enum):
    """
    MCP 传输协议枚举类
    
    定义 MCP 支持的传输方式，第一阶段支持两种：
    - STDIO: 标准输入输出（进程间通信）
    - HTTP: HTTP 协议（远程服务调用）
    """

    STDIO = "stdio"
    HTTP = "http"


class MCPServerConfig(BaseModel):
    """
    MCP 服务器配置类
    
    运行时管理器使用的可序列化 MCP 服务器配置
    包含连接 MCP 服务器所需的所有参数
    """

    id: UUID | None = None  # 服务器唯一标识
    account_id: UUID | None = None  # 所属账户ID
    name: str = ""  # 服务器名称
    description: str = ""  # 服务器描述
    transport: MCPTransport = MCPTransport.STDIO  # 传输协议
    enabled: bool = True  # 是否启用
    url: str = ""  # HTTP 传输时的服务器地址
    command: str = ""  # STDIO 传输时的启动命令
    args: list[str] = Field(default_factory=list)  # 启动命令参数
    env: dict[str, str] = Field(default_factory=dict)  # 环境变量
    headers: list[dict[str, str]] = Field(default_factory=list)  # HTTP 请求头
    timeout: int = 30  # 超时时间（秒）
    updated_at: datetime | None = None  # 最后更新时间


class MCPToolCatalog(BaseModel):
    """
    MCP 工具目录类
    
    持久化的 MCP 工具元数据，用于存储从 MCP 服务器发现的工具信息
    """

    name: str  # 工具名称
    description: str = ""  # 工具描述
    input_schema: dict[str, Any] = Field(default_factory=dict)  # 输入参数结构
    raw_definition: dict[str, Any] = Field(default_factory=dict)  # 原始工具定义