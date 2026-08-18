"""Synchronous platform SubAgent schema and native spec mapping tests."""

from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import Field

from internal.core.agent_v2.subagents.specs import (
    SubAgentSpecError,
    build_subagent_specs,
    validate_platform_subagents,
)


@tool
def web_search(query: str) -> str:
    """Search the web."""
    return query


def _sync(**overrides):
    value = {
        "name": "researcher",
        "description": "搜索并归纳资料",
        "system_prompt": "你是严谨的研究员。",
        "mode": "sync",
        "context_passing": "NONE",
        "tools": [],
        "skills": [],
    }
    value.update(overrides)
    return value


def test_empty_team_normalizes_to_list():
    assert validate_platform_subagents(None) == []
    assert validate_platform_subagents([]) == []
    assert validate_platform_subagents({}) == []


def test_legacy_dictionary_normalizes_to_sync_only_schema():
    config = validate_platform_subagents(
        {
            "researcher": {
                "description": "  搜索并归纳资料  ",
                "system_prompt": "  你是严谨的研究员。  ",
            }
        }
    )

    assert config == [_sync()]


def test_sync_spec_uses_explicit_tools_scoped_skills_and_read_only_filesystem():
    config = _sync(
        tools=["web_search"],
        model="openai:gpt-4o",
        skills=["research", "citations"],
    )

    spec = build_subagent_specs(
        [config],
        available_tools=[web_search],
        skill_sources_by_agent={
            "researcher": ["/subagent_skills/researcher/"]
        },
    )[0]

    assert spec["tools"] == [web_search]
    assert spec["model"] == "openai:gpt-4o"
    assert spec["skills"] == ["/subagent_skills/researcher/"]
    assert "自包含任务说明" in spec["description"]
    assert "看不到父会话历史" in spec["system_prompt"]
    assert spec["permissions"][0].operations == ["read"]
    assert spec["permissions"][0].paths == [
        "/subagent_skills/researcher/**"
    ]
    assert spec["permissions"][0].mode == "allow"
    assert "/conversation_history/**" in spec["permissions"][1].paths
    assert spec["permissions"][2].operations == ["read"]
    assert spec["permissions"][2].paths == ["/**"]
    assert spec["permissions"][2].mode == "deny"
    assert spec["permissions"][-1].operations == ["write"]
    assert spec["permissions"][-1].paths == ["/**"]
    assert spec["permissions"][-1].mode == "deny"

    from deepagents.middleware.filesystem import _check_fs_permission

    assert (
        _check_fs_permission(
            spec["permissions"],
            "read",
            "/subagent_skills/researcher/research/SKILL.md",
        )
        == "allow"
    )
    assert (
        _check_fs_permission(
            spec["permissions"],
            "read",
            "/subagent_skills/writer/report/SKILL.md",
        )
        == "deny"
    )
    assert (
        _check_fs_permission(
            spec["permissions"],
            "read",
            "/conversation_history/thread.md",
        )
        == "deny"
    )
    assert (
        _check_fs_permission(spec["permissions"], "read", "/workspace/out.md")
        == "deny"
    )
    assert _check_fs_permission(spec["permissions"], "read", "/unknown.md") == "deny"
    assert _check_fs_permission(spec["permissions"], "write", "/out.md") == "deny"


def test_empty_tool_list_is_forwarded_instead_of_inheriting_parent_tools():
    spec = build_subagent_specs([_sync()], available_tools=[web_search])[0]
    assert "tools" in spec
    assert spec["tools"] == []


def test_sync_spec_rejects_tools_not_authorized_on_parent():
    with pytest.raises(SubAgentSpecError, match="未授权工具"):
        build_subagent_specs([_sync(tools=["database_admin"])])


def test_configured_skills_require_a_runtime_authorization_projection():
    with pytest.raises(SubAgentSpecError, match="Skills 尚未完成运行时授权"):
        build_subagent_specs([_sync(skills=["research"])])


@pytest.mark.parametrize("mode", ["async", "ASYNC"])
def test_async_mode_is_rejected_until_the_async_phase(mode):
    with pytest.raises(SubAgentSpecError, match="当前仅支持 sync"):
        validate_platform_subagents([_sync(mode=mode)])


@pytest.mark.parametrize("policy", ["SUMMARY", "RECENT_K", "ALL"])
def test_parent_history_copy_policies_are_rejected(policy):
    with pytest.raises(SubAgentSpecError, match="context_passing=NONE"):
        validate_platform_subagents([_sync(context_passing=policy)])


@pytest.mark.parametrize("name", ["Researcher", "research agent", "_researcher"])
def test_invalid_or_duplicate_names_are_rejected(name):
    with pytest.raises(SubAgentSpecError, match="名称"):
        validate_platform_subagents([_sync(name=name)])

    with pytest.raises(SubAgentSpecError, match="重复"):
        validate_platform_subagents([_sync(), _sync()])


def test_unknown_legacy_async_policy_fields_and_large_teams_are_rejected():
    for field in (
        "graph_id",
        "url",
        "headers",
        "timeout_seconds",
        "max_concurrent_per_conversation",
        "k",
    ):
        with pytest.raises(SubAgentSpecError, match="当前同步阶段不支持"):
            validate_platform_subagents([_sync(**{field: "value"})])
    with pytest.raises(SubAgentSpecError, match="数量"):
        validate_platform_subagents(
            [_sync(name=f"agent-{index}") for index in range(9)]
        )


def test_factory_compiles_native_synchronous_subagent():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langgraph.graph.state import CompiledStateGraph

    from internal.core.agent_v2.runtime.factory import build_deep_agent

    agent = build_deep_agent(
        llm=FakeListChatModel(responses=["ok"]),
        tools=[],
        system_prompt="main agent",
        review_config={},
        checkpointer=False,
        subagents=[_sync()],
    )

    assert isinstance(agent, CompiledStateGraph)


class DelegationChatModel(BaseChatModel):
    calls: list[list[Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "delegation-chat-model"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(self, messages, stop=None, **kwargs):
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "return worker result",
                            "subagent_type": "researcher",
                        },
                        "id": "call-1",
                    }
                ],
            )
        elif len(self.calls) == 2:
            message = AIMessage(content="worker result")
        else:
            message = AIMessage(content="final answer")
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_native_synchronous_task_delegation_executes_without_parent_history():
    from internal.core.agent_v2.runtime.factory import build_deep_agent

    model = DelegationChatModel()
    agent = build_deep_agent(
        llm=model,
        tools=[],
        system_prompt="main agent",
        review_config={},
        checkpointer=False,
        subagents=[_sync()],
    )

    result = agent.invoke({"messages": [HumanMessage(content="delegate")]})

    assert result["messages"][-1].content == "final answer"
    assert len(model.calls) == 3
    worker_visible_messages = [
        message.content
        for message in model.calls[1]
        if not isinstance(message, SystemMessage)
    ]
    assert worker_visible_messages == ["return worker result"]
    assert "看不到父会话历史" in str(model.calls[1][0].content)
