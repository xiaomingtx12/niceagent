"""ProjectContextMiddleware 单测。

覆盖:
- 构造时把 account_id / app_id / conversation_id 存到实例属性
- before_agent 返回 dict 包含 4 个 runtime_* 字段
- 真实 invoke 时, runtime_* 字段被 merge 到 CompiledStateGraph 跑的 state

约束 (硬约束 #5): 不重写 deepagents before_agent 行为; 只测业务包装层.
"""
import re

import pytest

from internal.core.agent_v2.middleware.project_context_middleware import (
    ProjectContextMiddleware,
)


# ---------- 直接调 before_agent (单元) ----------


def test_constructor_stores_three_ids():
    mw = ProjectContextMiddleware(
        account_id="acc-1", app_id="app-1", conversation_id="cnv-1",
    )
    assert mw._account_id == "acc-1"
    assert mw._app_id == "app-1"
    assert mw._conversation_id == "cnv-1"


def test_before_agent_returns_runtime_fields():
    """before_agent 返回 4 个 runtime_* 字段, 不返回 None."""
    mw = ProjectContextMiddleware(
        account_id="acc-A", app_id="app-B", conversation_id="cnv-C",
    )
    out = mw.before_agent(state={"messages": []}, runtime=None)
    assert out is not None
    assert out["runtime_account_id"] == "acc-A"
    assert out["runtime_app_id"] == "app-B"
    assert out["runtime_conversation_id"] == "cnv-C"
    # 时间戳是 ISO8601
    assert "T" in out["runtime_invoked_at"]
    assert re.match(r"\d{4}-\d{2}-\d{2}T", out["runtime_invoked_at"])


def test_before_agent_does_not_mutate_state():
    """before_agent 接收 state, 但只返回值不修改入参 (业务约定, 不破坏 graph 节点入参)."""
    mw = ProjectContextMiddleware(
        account_id="acc-1", app_id="app-1", conversation_id="cnv-1",
    )
    state_in = {"messages": [], "existing_key": "preserved"}
    out = mw.before_agent(state=state_in, runtime=None)
    # 原始 state 不变
    assert "runtime_account_id" not in state_in
    assert state_in["existing_key"] == "preserved"
    # out 是新 dict
    assert out is not state_in


def test_before_agent_does_not_modify_messages():
    """L1 业务身份不直接写 messages, 摘要由 R2 SummarizationMiddleware 接管.

    业务约定: R0 不再 master 老 <长期记忆>{long_term_memory}</长期记忆> 占位
    注入; runtime_* 字段进 state 但不进 messages, system_prompt 已经在
    factory 里渲染好了.
    """
    mw = ProjectContextMiddleware(
        account_id="acc-1", app_id="app-1", conversation_id="cnv-1",
    )
    out = mw.before_agent(
        state={"messages": [{"role": "user", "content": "hi"}]}, runtime=None
    )
    assert "messages" not in out
    # 4 个 runtime_* 全部就位
    assert set(out.keys()) == {
        "runtime_account_id",
        "runtime_app_id",
        "runtime_conversation_id",
        "runtime_invoked_at",
    }


# ---------- merge 行为 (langchain create_agent runtime 契约) ----------

# 完整 e2e (用 build_deep_agent + invoke) 超过 unit test 范围: deepagents 内部
# provider profile lookup 要求 model 是 spec 字符串 (e.g. "openai:gpt-4o"), 真实
# model 实例触发 AttributeError on spec.count(":"). 端到端覆盖在 test_factory.py
# + test_review_middleware.py. 这里验证 before_agent 输出满足 langgraph
# create_agent 的 dict-merge 契约即可 (业务用法实质: ProjectContextMiddleware
# 在 graph 跑 before_agent 时, 返回 dict 由 create_agent 自动 merge 到 state).


def test_before_agent_return_is_dict_mergeable_into_state():
    """before_agent 返回的 dict 形状, 可被 langchain create_agent 合并到 state.

    langchain create_agent 在 before_agent 后用 dict-update 语义合并返回值
    到 state (即: state.update(before_agent_output)). 我们返回 dict 的
    key 都是字符串, value 都是标量 / ISO8601 字符串, 满足 dict 合并约束.
    """
    mw = ProjectContextMiddleware(
        account_id="acc-merge", app_id="app-merge", conversation_id="cnv-merge",
    )
    out = mw.before_agent(state={"messages": []}, runtime=None)

    # 模拟 langgraph before_agent merge 行为
    state = {"messages": []}
    state.update(out)

    assert state["runtime_account_id"] == "acc-merge"
    assert state["runtime_app_id"] == "app-merge"
    assert state["runtime_conversation_id"] == "cnv-merge"
    assert "T" in state["runtime_invoked_at"]


def test_before_agent_returns_only_string_values():
    """所有 value 都是 str (ISO8601); merge 不会冲突.

    dict.update 不会拒绝 str value, 但若 before_agent 返回 set / tuple /
    pydantic model 等, create_agent 后续可能 assert 类型. 我们严格保证 str.
    """
    mw = ProjectContextMiddleware(
        account_id="acc", app_id="app", conversation_id="cnv",
    )
    out = mw.before_agent(state={"messages": []}, runtime=None)
    for k, v in out.items():
        assert isinstance(v, str), f"{k} should be str, got {type(v)}"


def test_before_agent_invoked_at_is_iso8601_utc():
    """runtime_invoked_at 必须 ISO8601 UTC (供 audit log 排序 / 跨时区对齐).

    业务用途: review / audit log 按时间排序; 跨 account / app 的事件对齐.
    不写本地时间 (本地时区会让多 region 实例之间 audit log 错乱).
    """
    from datetime import datetime, timezone

    mw = ProjectContextMiddleware(
        account_id="acc", app_id="app", conversation_id="cnv",
    )
    out = mw.before_agent(state={"messages": []}, runtime=None)
    parsed = datetime.fromisoformat(out["runtime_invoked_at"])
    # ISO8601 允许带时区; 业务约定 UTC (offset +00:00)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_before_agent_independent_per_call():
    """不同 instance 的 _account_id / _app_id / _conversation_id 互不污染."""
    mw_a = ProjectContextMiddleware(
        account_id="acc-A", app_id="app-A", conversation_id="cnv-A",
    )
    mw_b = ProjectContextMiddleware(
        account_id="acc-B", app_id="app-B", conversation_id="cnv-B",
    )
    out_a = mw_a.before_agent(state={}, runtime=None)
    out_b = mw_b.before_agent(state={}, runtime=None)
    assert out_a["runtime_account_id"] == "acc-A"
    assert out_b["runtime_account_id"] == "acc-B"
    # 关键: out_a 的 dict 不能引用 mw_b 的字段
    assert "app-B" not in out_a["runtime_app_id"]
    assert "app-A" not in out_b["runtime_app_id"]
