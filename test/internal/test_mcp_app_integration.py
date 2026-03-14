from types import SimpleNamespace
from uuid import uuid4

from internal.model import MCPServer, MCPTool
from internal.service.app_config_service import AppConfigService
from internal.service.app_service import AppService


class FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *args, **kwargs):
        return self

    def one_or_none(self):
        return self._result


class FakeSession:
    def __init__(self, query_result):
        self._query_result = query_result

    def query(self, model):
        return FakeQuery(self._query_result)


class FakeDB:
    def __init__(self, query_result):
        self.session = FakeSession(query_result)


class FakeRuntimeManager:
    def __init__(self):
        self.calls = []

    def get_langchain_tool(self, server, tool_name):
        self.calls.append((server.id, tool_name))
        return SimpleNamespace(name=tool_name, server_id=str(server.id))


def test_app_config_service_builds_langchain_mcp_tools():
    server_id = uuid4()
    server = SimpleNamespace(id=server_id, enabled=True)
    runtime_manager = FakeRuntimeManager()
    service = AppConfigService(
        db=FakeDB(None),
        api_provider_manager=None,
        builtin_provider_manager=None,
        language_model_manager=None,
        mcp_runtime_manager=runtime_manager,
    )
    service.get = lambda model, primary_key: server if model is MCPServer else None

    tools = service.get_langchain_tools_by_tools_config(
        [
            {
                "type": "mcp_tool",
                "provider": {"id": str(server_id)},
                "tool": {"name": "weather"},
            }
        ]
    )

    assert len(tools) == 1
    assert tools[0].name == "weather"
    assert runtime_manager.calls == [(server_id, "weather")]


def test_app_config_service_processes_mcp_tool_display_payload():
    server_id = uuid4()
    server = SimpleNamespace(
        id=server_id,
        name="weather-server",
        description="Weather tools",
        enabled=True,
    )
    tool_record = SimpleNamespace(
        id=uuid4(),
        name="weather",
        description="Get weather",
    )
    service = AppConfigService(
        db=FakeDB(tool_record),
        api_provider_manager=None,
        builtin_provider_manager=None,
        language_model_manager=None,
        mcp_runtime_manager=FakeRuntimeManager(),
    )
    service.get = lambda model, primary_key: server if model is MCPServer else None

    tools, validate_tools = service._process_and_validate_tools(
        [
            {
                "type": "mcp_tool",
                "provider_id": str(server_id),
                "tool_id": "weather",
                "params": {},
            }
        ]
    )

    assert validate_tools == [
        {
            "type": "mcp_tool",
            "provider_id": str(server_id),
            "tool_id": "weather",
            "params": {},
        }
    ]
    assert tools[0]["provider"]["name"] == "weather-server"
    assert tools[0]["tool"]["name"] == "weather"


def test_app_service_accepts_mcp_tool_in_draft_config():
    account_id = uuid4()
    server_id = uuid4()
    app_service = AppService(
        db=FakeDB(SimpleNamespace(name="weather")),
        redis_client=None,
        retrieval_service=None,
        api_provider_manager=None,
        builtin_provider_manager=SimpleNamespace(get_tool=lambda *args, **kwargs: None),
        cos_service=None,
        conversation_service=None,
        app_config_service=None,
        language_model_service=None,
        language_model_manager=None,
        provider_api_key_service=None,
    )
    app_service.get = lambda model, primary_key: (
        SimpleNamespace(id=server_id, account_id=account_id, enabled=True)
        if model is MCPServer
        else None
    )

    result = app_service._validate_draft_app_config(
        {
            "tools": [
                {
                    "type": "mcp_tool",
                    "provider_id": str(server_id),
                    "tool_id": "weather",
                    "params": {},
                }
            ]
        },
        account=SimpleNamespace(id=account_id),
    )

    assert result["tools"][0]["type"] == "mcp_tool"
    assert result["tools"][0]["tool_id"] == "weather"
