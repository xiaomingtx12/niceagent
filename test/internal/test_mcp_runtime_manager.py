from datetime import datetime
from uuid import uuid4

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from internal.core.mcp.entities import MCPServerConfig, MCPTransport
from internal.core.mcp.runtime import MCPRuntimeManager


class FakeArgsSchema(BaseModel):
    city: str = Field(description="City name")


class FakeRemoteTool(BaseTool):
    name: str = "weather"
    description: str = "Get weather"
    args_schema: type[BaseModel] = FakeArgsSchema

    def _run(self, *args, **kwargs):
        return {"city": kwargs["city"], "weather": "sunny"}


def test_build_client_config_for_http_server():
    manager = MCPRuntimeManager()
    config = MCPServerConfig(
        id=uuid4(),
        name="remote-weather",
        transport=MCPTransport.HTTP,
        url="http://localhost:8000/mcp",
        headers=[{"key": "Authorization", "value": "Bearer token"}],
        updated_at=datetime(2026, 3, 14),
    )

    client_config = manager._build_client_config(config)

    assert client_config == {
        "remote-weather": {
            "transport": "http",
            "url": "http://localhost:8000/mcp",
            "headers": {"Authorization": "Bearer token"},
        }
    }


def test_list_langchain_tools_uses_process_cache(monkeypatch):
    manager = MCPRuntimeManager()
    server_config = MCPServerConfig(
        id=uuid4(),
        name="local-weather",
        transport=MCPTransport.STDIO,
        command="python",
        args=["server.py"],
        updated_at=datetime(2026, 3, 14, 12, 0, 0),
    )
    calls = {"count": 0}

    async def fake_load_remote_tools(_server_config):
        calls["count"] += 1
        return [FakeRemoteTool()]

    monkeypatch.setattr(manager, "_load_remote_tools_async", fake_load_remote_tools)

    tools_first = manager.list_langchain_tools(server_config)
    tools_second = manager.list_langchain_tools(server_config)

    assert calls["count"] == 1
    assert len(tools_first) == 1
    assert tools_first[0] is tools_second[0]
    assert tools_first[0].name == "weather"


def test_execute_tool_uses_remote_tool(monkeypatch):
    manager = MCPRuntimeManager()
    server_config = MCPServerConfig(
        id=uuid4(),
        name="local-weather",
        transport=MCPTransport.STDIO,
        command="python",
        args=["server.py"],
        updated_at=datetime(2026, 3, 14, 12, 0, 0),
    )

    async def fake_load_remote_tools(_server_config):
        return [FakeRemoteTool()]

    monkeypatch.setattr(manager, "_load_remote_tools_async", fake_load_remote_tools)

    result = manager.execute_tool(
        server_config=server_config,
        tool_name="weather",
        parameters={"city": "Beijing"},
    )

    assert result == {"city": "Beijing", "weather": "sunny"}
