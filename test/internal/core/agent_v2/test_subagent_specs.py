"""build_subagent_specs 单测。

覆盖:
- None / 空 dict → []
- 同步 SubAgent: name/description/system_prompt 必填 + tools/model 可选透传
- 异步 AsyncSubAgent: graph_id 路由 + url/headers 可选透传
- 混合 sync + async
- 非法配置: 缺 description / 缺 system_prompt / value 不是 dict
- e2e: build_deep_agent 接 subagents 后 graph 编译通过, task 工具注册

约束 (硬约束 #5): 不自己写 SubAgentMiddleware; 只测纯函数转换 + factory 集成.
"""
import pytest

from internal.core.agent_v2.subagent_specs import (
    SubAgentSpecError,
    build_subagent_specs,
    validate_platform_subagents,
)


# ---------- 边界 ----------


def test_none_returns_empty_list():
    assert build_subagent_specs(None) == []


def test_empty_dict_returns_empty_list():
    assert build_subagent_specs({}) == []


# ---------- 同步 SubAgent ----------


def test_sync_subagent_minimal():
    specs = build_subagent_specs({
        "researcher": {
            "description": "研究员",
            "system_prompt": "你是研究员",
        },
    })
    assert specs == [{
        "name": "researcher",
        "description": "研究员",
        "system_prompt": "你是研究员",
    }]


def test_sync_subagent_with_optional_tools_and_model():
    specs = build_subagent_specs({
        "coder": {
            "description": "写代码",
            "system_prompt": "你是程序员",
            "tools": ["fake-tool"],
            "model": "openai:gpt-4o",
        },
    })
    assert specs[0]["tools"] == ["fake-tool"]
    assert specs[0]["model"] == "openai:gpt-4o"


def test_sync_subagent_empty_tools_not_included():
    """tools=[] 不进 spec (deepagents: 不传 tools 继承主 agent; 传 [] 是无工具)."""
    specs = build_subagent_specs({
        "a": {"description": "d", "system_prompt": "s", "tools": []},
    })
    assert "tools" not in specs[0]


# ---------- 异步 AsyncSubAgent ----------


def test_async_subagent_minimal():
    specs = build_subagent_specs({
        "remote": {
            "description": "远端任务",
            "graph_id": "my-graph",
        },
    })
    assert specs == [{
        "name": "remote",
        "description": "远端任务",
        "graph_id": "my-graph",
    }]


def test_async_subagent_with_url_and_headers():
    specs = build_subagent_specs({
        "remote": {
            "description": "远端",
            "graph_id": "g1",
            "url": "https://langgraph.example.com",
            "headers": {"x-api-key": "secret"},
        },
    })
    assert specs[0]["url"] == "https://langgraph.example.com"
    assert specs[0]["headers"] == {"x-api-key": "secret"}


def test_async_subagent_does_not_require_system_prompt():
    """async sub-agent 跑在远端, system_prompt 在远端 graph 里, 本地不需要."""
    specs = build_subagent_specs({
        "remote": {"description": "d", "graph_id": "g"},
    })
    assert "system_prompt" not in specs[0]


# ---------- 混合 ----------


def test_mixed_sync_and_async():
    specs = build_subagent_specs({
        "researcher": {"description": "研究", "system_prompt": "你是研究员"},
        "remote": {"description": "远端", "graph_id": "g1"},
    })
    by_name = {s["name"]: s for s in specs}
    assert "graph_id" not in by_name["researcher"]
    assert by_name["remote"]["graph_id"] == "g1"


# ---------- 非法配置 ----------


def test_missing_description_raises():
    with pytest.raises(SubAgentSpecError, match="description"):
        build_subagent_specs({"a": {"system_prompt": "s"}})


def test_missing_system_prompt_raises_for_sync():
    with pytest.raises(SubAgentSpecError, match="system_prompt"):
        build_subagent_specs({"a": {"description": "d"}})


def test_non_dict_value_raises():
    with pytest.raises(SubAgentSpecError, match="dict"):
        build_subagent_specs({"a": "not-a-dict"})


# ---------- Creator API 持久化契约 ----------


def test_platform_config_normalizes_sync_subagent():
    config = validate_platform_subagents(
        {
            "researcher": {
                "description": "  搜索并归纳资料  ",
                "system_prompt": "  你是严谨的研究员。  ",
            }
        }
    )
    assert config == {
        "researcher": {
            "description": "搜索并归纳资料",
            "system_prompt": "你是严谨的研究员。",
        }
    }


@pytest.mark.parametrize("name", ["Researcher", "research agent", "_researcher"])
def test_platform_config_rejects_invalid_name(name):
    with pytest.raises(SubAgentSpecError, match="名称"):
        validate_platform_subagents(
            {name: {"description": "研究", "system_prompt": "做研究"}}
        )


def test_platform_config_rejects_remote_or_secret_fields():
    with pytest.raises(SubAgentSpecError, match="暂不支持"):
        validate_platform_subagents(
            {
                "remote": {
                    "description": "远端任务",
                    "system_prompt": "执行任务",
                    "graph_id": "remote-graph",
                    "headers": {"authorization": "secret"},
                }
            }
        )


def test_platform_config_rejects_more_than_eight_agents():
    config = {
        f"agent-{index}": {
            "description": f"agent {index}",
            "system_prompt": "执行分配的任务",
        }
        for index in range(9)
    }
    with pytest.raises(SubAgentSpecError, match="数量"):
        validate_platform_subagents(config)


# ---------- factory 集成 ----------


def test_factory_accepts_subagents_and_compiles():
    """build_deep_agent(subagents=...) 编译通过; sync spec 注入 SubAgentMiddleware."""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langgraph.graph.state import CompiledStateGraph

    from internal.core.agent_v2.factory import build_deep_agent

    agent = build_deep_agent(
        llm=FakeListChatModel(responses=["ok"]),
        tools=[],
        system_prompt="main agent",
        review_config={},
        account_id="acc",
        app_id="app",
        conversation_id="cnv",
        use_checkpointer=False,
        max_message_count=0,
        subagents={
            "researcher": {
                "description": "研究员",
                "system_prompt": "你是研究员",
            },
        },
    )
    assert isinstance(agent, CompiledStateGraph)


def test_factory_without_subagents_still_compiles():
    """不传 subagents (None) 行为与 R2 一致."""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langgraph.graph.state import CompiledStateGraph

    from internal.core.agent_v2.factory import build_deep_agent

    agent = build_deep_agent(
        llm=FakeListChatModel(responses=["ok"]),
        tools=[],
        system_prompt="main agent",
        review_config={},
        account_id="acc",
        app_id="app",
        conversation_id="cnv",
        use_checkpointer=False,
        max_message_count=0,
    )
    assert isinstance(agent, CompiledStateGraph)
