from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class MCPTransport(str, Enum):
    """Supported MCP transports in phase 1."""

    STDIO = "stdio"
    HTTP = "http"


class MCPServerConfig(BaseModel):
    """Serializable MCP server config used by the runtime manager."""

    id: UUID | None = None
    account_id: UUID | None = None
    name: str = ""
    description: str = ""
    transport: MCPTransport = MCPTransport.STDIO
    enabled: bool = True
    url: str = ""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    headers: list[dict[str, str]] = Field(default_factory=list)
    timeout: int = 30
    updated_at: datetime | None = None


class MCPToolCatalog(BaseModel):
    """Persisted MCP tool metadata."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    raw_definition: dict[str, Any] = Field(default_factory=dict)
