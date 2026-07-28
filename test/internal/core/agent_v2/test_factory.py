"""factory.build_deep_agent 集成单测（不依赖 DB / LLM）。

用 FakeChatModel + 空 tools 跑 create_deep_agent，确认：
- 返回 CompiledStateGraph
- 4 个业务中间件都装上了
- state_schema 是 MyDifyDeepAgentState

约束：单测环境不能真连 Postgres——传 use_checkpointer=False
绕过 saver；model 用 langchain 自带的 FakeListChatModel。
"""
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from internal.core.agent_v2 import factory
from internal.core.agent_v2.factory import (
    MyDifyDeepAgentState,
    build_deep_agent,
    build_thread_config,
)


def _llm():
    """Fake LLM：第一次返回 AIMessage（不调工具）；用于跑通 graph。"""
    return FakeListChatModel(responses=["hello"])


def test_build_deep_agent_returns_compiled_state_graph():
    from langgraph.graph.state import CompiledStateGraph
    agent = build_deep_agent(
        llm=_llm(),
        tools=[],
        system_prompt="test prompt",
        review_config={"enable": False},
        account_id="acct1",
        app_id="app1",
        conversation_id="cnv1",
        use_checkpointer=False,
    )
    assert isinstance(agent, CompiledStateGraph)


def test_empty_team_does_not_expose_implicit_general_purpose_task():
    agent = build_deep_agent(
        llm=_llm(),
        tools=[],
        system_prompt="test prompt",
        review_config={},
        account_id="acct1",
        app_id="app1",
        conversation_id="cnv1",
        use_checkpointer=False,
        subagents=[],
    )

    tool_names = set(agent.nodes["tools"].bound._tools_by_name)
    assert "task" not in tool_names


def test_explicit_sync_team_exposes_native_task_tool():
    agent = build_deep_agent(
        llm=_llm(),
        tools=[],
        system_prompt="test prompt",
        review_config={},
        account_id="acct1",
        app_id="app1",
        conversation_id="cnv1",
        use_checkpointer=False,
        subagents=[
            {
                "name": "researcher",
                "description": "research",
                "system_prompt": "work carefully",
            }
        ],
    )

    tool_names = set(agent.nodes["tools"].bound._tools_by_name)
    assert "task" in tool_names


def test_build_deep_agent_with_review_enabled_keeps_going():
    """review enable=True 但没关键词时不影响调用。"""
    agent = build_deep_agent(
        llm=_llm(),
        tools=[],
        system_prompt="test prompt",
        review_config={"enable": True, "keywords": [], "inputs_config": {"enable": True}, "outputs_config": {"enable": True}},
        account_id="acct1",
        app_id="app1",
        conversation_id="cnv1",
        use_checkpointer=False,
    )
    # deepagents 1.x 用 builder.schemas (dict[schema_cls, channels])；用户的
    # state_schema 字段被合并进 StateSchema/InputSchema/OutputSchema 的 channels
    schemas = getattr(agent.builder, "schemas", None)
    assert schemas is not None
    # 拿 StateSchema 的 channels dict，里面应当包含 deepagents 扩展字段 + 我们
    # 后续可注入的字段；这里只验证 'messages' / 'todos' / 'files' 三个 deepagents
    # 内置字段都在
    all_channels = set()
    for ch_dict in schemas.values():
        all_channels.update(ch_dict.keys())
    assert "messages" in all_channels
    assert "todos" in all_channels
    assert "files" in all_channels
    assert "runtime_account_id" in all_channels
    assert "runtime_review_blocked" in all_channels


def test_runtime_business_context_is_private_to_the_parent_agent():
    from deepagents.middleware._state import private_state_field_names

    private_fields = private_state_field_names(MyDifyDeepAgentState)

    assert "runtime_workflow_variables" in private_fields
    assert "runtime_end_user_id" in private_fields


def test_build_deep_agent_routes_backend_through_native_contract(monkeypatch):
    from deepagents.backends import StateBackend

    captured = {}
    sentinel = object()

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return sentinel

    backend = StateBackend()
    monkeypatch.setattr(factory, "create_deep_agent", fake_create_deep_agent)

    result = build_deep_agent(
        llm=_llm(),
        tools=[],
        system_prompt="test prompt",
        review_config={},
        account_id="acct1",
        app_id="app1",
        conversation_id="cnv1",
        workspace_backend=backend,
        use_checkpointer=False,
    )

    assert result is sentinel
    assert captured["backend"] is backend
    assert captured["memory"] is None
    assert captured["permissions"] == []
    names = [middleware.name for middleware in captured["middleware"]]
    assert "EventTranslationMiddleware" in names
    assert "FilesystemMiddleware" not in names
    # Filesystem/Summarization 均由 create_deep_agent 的默认栈安装。
    assert "SummarizationMiddleware" not in names


def test_build_deep_agent_passes_native_memory_and_permissions(monkeypatch):
    from deepagents.middleware.filesystem import FilesystemPermission

    captured = {}
    sentinel = object()

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return sentinel

    permission = FilesystemPermission(
        operations=["read"],
        paths=["/memories/**"],
        mode="allow",
    )
    monkeypatch.setattr(factory, "create_deep_agent", fake_create_deep_agent)

    result = build_deep_agent(
        llm=_llm(),
        tools=[],
        system_prompt="test prompt",
        review_config={},
        account_id="acct1",
        app_id="app1",
        conversation_id="cnv1",
        memory_sources=["/memories/profile.md"],
        permissions=[permission],
        use_checkpointer=False,
    )

    assert result is sentinel
    assert captured["memory"] == ["/memories/profile.md"]
    assert captured["permissions"] == [permission]


def test_build_deep_agent_rejects_conflicting_checkpointer_options():
    with pytest.raises(ValueError, match="cannot be provided together"):
        build_deep_agent(
            llm=_llm(),
            tools=[],
            system_prompt="test prompt",
            review_config={},
            account_id="acct1",
            app_id="app1",
            conversation_id="cnv1",
            checkpointer=True,
            use_checkpointer=False,
        )


def test_build_thread_config_contains_thread_id_and_metadata():
    cfg = build_thread_config(
        account_id="acct1", app_id="app1", conversation_id="cnv1"
    )
    assert cfg["configurable"]["thread_id"] == "(acct1,app1,cnv1,main)"
    assert cfg["metadata"]["account_id"] == "acct1"
    assert cfg["metadata"]["app_id"] == "app1"
    assert cfg["metadata"]["conversation_id"] == "cnv1"
    assert cfg["metadata"]["agent_id"] == "main"


def test_build_thread_config_with_subagent_id():
    cfg = build_thread_config(
        account_id="acct1",
        app_id="app1",
        conversation_id="cnv1",
        agent_id="researcher",
    )
    assert cfg["configurable"]["thread_id"] == "(acct1,app1,cnv1,researcher)"
    assert cfg["metadata"]["agent_id"] == "researcher"
