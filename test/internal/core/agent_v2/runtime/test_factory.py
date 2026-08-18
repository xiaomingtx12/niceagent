"""DeepAgents 图装配契约测试。"""

from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from deepagents.backends import CompositeBackend
from deepagents.backends import StoreBackend
from deepagents.backends.utils import create_file_data
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox
from pydantic import Field

from internal.core.agent_v2.runtime import factory
from internal.core.agent_v2.backends.deny import DenyBackend
from internal.core.agent_v2.runtime.factory import (
    build_deep_agent,
    build_thread_config,
)
from internal.core.agent_v2.context.permissions import (
    build_filesystem_permissions,
)


def _llm():
    """返回无需外部服务的聊天模型。"""
    return FakeListChatModel(responses=["hello"])


class _RecordingChatModel(BaseChatModel):
    """记录每次模型调用可见的工具。"""

    bound_tool_names: list[list[str]] = Field(default_factory=list)
    seen_messages: list[list] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        """返回稳定的测试模型类型。"""
        return "recording-chat-model"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """记录绑定工具并返回当前模型。"""
        self.bound_tool_names.append([tool.name for tool in tools])
        return self

    def _generate(self, messages, stop=None, **kwargs):
        """返回终止消息。"""
        self.seen_messages.append(messages)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="done"))]
        )


def test_build_deep_agent_returns_compiled_state_graph():
    """默认参数可以构造原生状态图。"""
    from langgraph.graph.state import CompiledStateGraph

    agent = build_deep_agent(
        llm=_llm(),
        tools=[],
        system_prompt="test prompt",
        review_config={"enable": False},
        checkpointer=False,
    )

    assert isinstance(agent, CompiledStateGraph)


def test_empty_team_excludes_implicit_general_purpose_tools():
    """空团队不暴露隐式委派和任意执行工具。"""
    model = _RecordingChatModel()
    agent = build_deep_agent(
        llm=model,
        tools=[],
        system_prompt="test prompt",
        review_config={},
        checkpointer=False,
        subagents=[],
    )

    agent.invoke({"messages": [HumanMessage(content="hello")]})

    tool_names = set(agent.nodes["tools"].bound._tools_by_name)
    assert "task" not in tool_names
    assert model.bound_tool_names
    assert "execute" not in model.bound_tool_names[-1]


class _SandboxBackend(BaseSandbox):
    @property
    def id(self):
        return "test-sandbox"

    def execute(self, command: str, *, timeout=None):
        return ExecuteResponse(output="", exit_code=0, truncated=False)

    def upload_files(self, files):
        return [FileUploadResponse(path=path) for path, _ in files]

    def download_files(self, paths):
        return [FileDownloadResponse(path=path, content=b"") for path in paths]


class _SandboxRecordingChatModel(_RecordingChatModel):
    """使用独立 provider 名称，避免跨测试 Harness Profile 串扰。"""


def test_sandbox_default_backend_exposes_native_execute():
    model = _SandboxRecordingChatModel()
    permissions = build_filesystem_permissions(sandbox_enabled=True)
    agent = build_deep_agent(
        llm=model,
        tools=[],
        system_prompt="test prompt",
        review_config={},
        checkpointer=False,
        backend=CompositeBackend(
            default=_SandboxBackend(),
            routes={},
        ),
        permissions=permissions,
        subagents=[],
    )

    agent.invoke({"messages": [HumanMessage(content="hello")]})

    assert permissions == []
    assert any("execute" in names for names in model.bound_tool_names)


def test_sandbox_subagent_compiles_with_native_backend():
    model = _SandboxRecordingChatModel()
    agent = build_deep_agent(
        llm=model,
        tools=[],
        system_prompt="test prompt",
        review_config={},
        checkpointer=False,
        backend=CompositeBackend(
            default=_SandboxBackend(),
            routes={},
        ),
        permissions=[],
        subagents=[
            {
                "name": "researcher",
                "description": "research",
                "system_prompt": "work carefully",
            }
        ],
    )

    assert agent is not None


def test_explicit_sync_team_exposes_native_task_tool():
    """显式同步团队启用 DeepAgents 原生 task 工具。"""
    agent = build_deep_agent(
        llm=_llm(),
        tools=[],
        system_prompt="test prompt",
        review_config={},
        checkpointer=False,
        subagents=[
            {
                "name": "researcher",
                "description": "research",
                "system_prompt": "work carefully",
            }
        ],
    )

    assert "task" in set(agent.nodes["tools"].bound._tools_by_name)


def test_graph_uses_native_state_without_business_context_channels():
    """图状态只保留 DeepAgents 原生通道。"""
    agent = build_deep_agent(
        llm=_llm(),
        tools=[],
        system_prompt="test prompt",
        review_config={"enable": True, "keywords": []},
        checkpointer=False,
    )
    schemas = getattr(agent.builder, "schemas", None)

    assert schemas is not None
    channels = {
        name
        for schema_channels in schemas.values()
        for name in schema_channels
    }
    assert {"messages", "todos", "files"} <= channels
    assert not any(name.startswith("runtime_") for name in channels)


def test_build_deep_agent_routes_native_runtime_options(monkeypatch):
    """业务装配参数直接传给原生 DeepAgents 工厂。"""
    from deepagents.backends import StateBackend
    from deepagents.middleware.filesystem import FilesystemPermission

    captured = {}
    sentinel = object()
    backend = StateBackend()
    permission = FilesystemPermission(
        operations=["read"],
        paths=["/memories/**"],
        mode="allow",
    )

    def fake_create_deep_agent(**kwargs):
        """捕获传入原生工厂的参数。"""
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(factory, "create_deep_agent", fake_create_deep_agent)

    result = build_deep_agent(
        llm=_llm(),
        tools=[],
        system_prompt="test prompt",
        review_config={},
        max_model_calls=7,
        checkpointer=False,
        backend=backend,
        memory_sources=["/memories/AGENTS.md"],
        context_sources=["/context/request.md"],
        permissions=[permission],
    )

    assert result is sentinel
    assert captured["backend"] is backend
    assert captured["checkpointer"] is False
    assert captured["memory"] == ["/memories/AGENTS.md"]
    assert captured["store"] is None
    assert captured["permissions"] == [permission]
    middleware = {item.name: item for item in captured["middleware"]}
    assert "EventTranslationMiddleware" in middleware
    assert middleware["ModelCallLimitMiddleware"].run_limit == 7
    assert middleware["ModelCallLimitMiddleware"].exit_behavior == "end"
    assert "state_schema" not in captured


def test_native_memory_middleware_loads_store_backend_file():
    """原生 MemoryMiddleware 从 StoreBackend 读取 AGENTS.md。"""
    from langgraph.store.memory import InMemoryStore

    model = _RecordingChatModel()
    store = InMemoryStore()
    namespace = ("mydify", "account", "app", "end-user")
    store.put(
        namespace,
        "/AGENTS.md",
        create_file_data("# Long-term memory\n\n- [preference] 中文\n"),
    )
    backend = CompositeBackend(
        default=DenyBackend(),
        routes={
            "/memories/": StoreBackend(
                store=store,
                namespace=lambda _runtime: namespace,
            )
        },
    )

    agent = build_deep_agent(
        llm=model,
        tools=[],
        system_prompt="test prompt",
        review_config={},
        checkpointer=False,
        backend=backend,
        store=store,
        memory_sources=["/memories/AGENTS.md"],
        permissions=[],
        subagents=[],
    )
    agent.invoke({"messages": [HumanMessage(content="hello")]})

    rendered = "\n".join(
        str(message.content)
        for message in model.seen_messages[0]
        if getattr(message, "type", None) == "system"
    )
    assert "中文" in rendered


def test_build_thread_config_contains_stable_tenant_metadata():
    """线程配置包含租户作用域和 Agent 标识。"""
    config = build_thread_config(
        account_id="acct1",
        app_id="app1",
        conversation_id="cnv1",
        agent_id="researcher",
    )

    assert config["configurable"]["thread_id"] == (
        "(acct1,app1,cnv1,researcher)"
    )
    assert config["metadata"] == {
        "account_id": "acct1",
        "app_id": "app1",
        "conversation_id": "cnv1",
        "agent_id": "researcher",
    }
