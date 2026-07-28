"""Layered L3 context middleware tests."""

from datetime import datetime
import re

from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from internal.core.agent_v2.middleware.project_context_middleware import (
    ProjectContextMiddleware,
)


def _middleware():
    return ProjectContextMiddleware(
        account_id="acc-1",
        app_id="app-1",
        conversation_id="cnv-1",
        end_user_id="user-1",
        workflow_variables={"region": "cn", "priority": 2},
    )


def test_before_agent_emits_complete_runtime_state_without_mutating_messages():
    middleware = _middleware()
    original = {"messages": [HumanMessage(content="hello")]}

    output = middleware.before_agent(original, runtime=None)

    assert original == {"messages": [HumanMessage(content="hello")]}
    assert "messages" not in output
    assert output["runtime_account_id"] == "acc-1"
    assert output["runtime_app_id"] == "app-1"
    assert output["runtime_conversation_id"] == "cnv-1"
    assert output["runtime_end_user_id"] == "user-1"
    assert output["runtime_workflow_variables"] == {
        "region": "cn",
        "priority": 2,
    }
    assert re.match(r"\d{4}-\d{2}-\d{2}T", output["runtime_invoked_at"])
    parsed = datetime.fromisoformat(output["runtime_invoked_at"])
    assert parsed.utcoffset().total_seconds() == 0


def test_before_agent_returns_fresh_collections_each_time():
    middleware = _middleware()

    first = middleware.before_agent({}, runtime=None)
    first["runtime_workflow_variables"]["region"] = "mutated"
    second = middleware.before_agent({}, runtime=None)

    assert second["runtime_workflow_variables"]["region"] == "cn"


def test_wrap_model_call_appends_dynamic_context_after_stable_system_message():
    middleware = _middleware()
    request = ModelRequest(
        model=FakeListChatModel(responses=["ok"]),
        messages=[HumanMessage(content="question")],
        system_message=SystemMessage(content="STABLE-L1"),
        state={
            "runtime_invoked_at": "2026-07-28T12:00:00+00:00",
            "runtime_workflow_variables": {"region": "cn"},
        },
    )
    captured = {}

    def handler(updated):
        captured["request"] = updated
        return "done"

    assert middleware.wrap_model_call(request, handler) == "done"
    content = captured["request"].system_message.content
    rendered = str(content)
    assert rendered.index("STABLE-L1") < rendered.index("<运行时上下文>")
    assert "当前时间: 2026-07-28T12:00:00+00:00" in rendered
    assert "终端用户标识: user-1" in rendered
    assert "用户偏好中文回复" not in rendered


def test_empty_optional_context_stays_valid():
    middleware = ProjectContextMiddleware(
        account_id="acc",
        app_id="app",
        conversation_id="cnv",
    )

    output = middleware.before_agent({}, runtime=None)

    assert output["runtime_end_user_id"] is None
    assert output["runtime_workflow_variables"] == {}
