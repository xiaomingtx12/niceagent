"""Verify opt-in planning through DTOs and the native Deep Agents todo loop."""

import asyncio
from uuid import uuid4

import pytest
from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field, ValidationError

from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.middleware.runtime_context_middleware import RuntimeContextMiddleware
from internal.entity.conversation_entity import InvokeFrom
from internal.schema.agent_runtime_schema import AgentInvocation
from internal.schema.agent_workbench_schema import WorkbenchCommandInputReq


@pytest.mark.parametrize("enabled", [False, True])
def test_planning_survives_command_and_invocation_restore(enabled):
    """The preference must survive the same JSON round trip used for run resume."""
    run_input = WorkbenchCommandInputReq(query="Prepare a report", planning_enabled=enabled).to_run_input()
    owner = uuid4()
    invocation = AgentInvocation(
        account_id=owner, app_id=uuid4(), conversation_id=uuid4(), created_by=owner,
        end_user_id=None, invoke_from=InvokeFrom.DEBUGGER, message_id=uuid4(),
        **run_input.model_dump(exclude={"file_ids"}),
    )
    restored = AgentInvocation.model_validate_json(invocation.model_dump_json())
    assert restored.planning_enabled is enabled
    assert WorkbenchCommandInputReq(query="hello").to_run_input().planning_enabled is False


@pytest.mark.parametrize("invalid", ["false", "true", 0, 1, None, {}])
def test_planning_requires_a_boolean(invalid):
    """Reject ambiguous payloads instead of unexpectedly enabling planning."""
    with pytest.raises(ValidationError):
        WorkbenchCommandInputReq(query="hello", planning_enabled=invalid)


class PlanningModel(FakeMessagesListChatModel):
    """Capture prompts while exercising real native write_todos calls locally."""

    observed_systems: list[str] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        """Keep scripted responses independent of external model providers."""
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """Record only system messages to detect preference leakage between runs."""
        self.observed_systems.append(str(messages[0].content))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.mark.parametrize("use_async", [False, True])
def test_native_todos_and_per_run_prompt_isolation(use_async):
    """Sync and async paths preserve native todos and clear opt-in on the next run."""
    todo = {"content": "Prepare report", "status": "in_progress"}
    completed = {**todo, "status": "completed"}
    model = PlanningModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "write_todos", "args": {"todos": [todo]}, "id": "plan"}]),
        AIMessage(content="", tool_calls=[{"name": "write_todos", "args": {"todos": [completed]}, "id": "done"}]),
        AIMessage(content="Report ready"),
        AIMessage(content="Hello"),
    ])
    graph = create_deep_agent(
        model=model, middleware=[RuntimeContextMiddleware()],
        context_schema=AgentRunContext, checkpointer=InMemorySaver(),
    )
    owner = uuid4()
    context = AgentRunContext(
        account_id=owner, app_id=uuid4(), conversation_id=uuid4(),
        created_by=owner, principal_id=owner, planning_enabled=True,
    )
    config = context.conversation.to_config()

    def invoke(text, runtime_context):
        """Use a single persisted conversation for both execution paths."""
        payload = {"messages": [HumanMessage(content=text)]}
        if use_async:
            return asyncio.run(graph.ainvoke(payload, config=config, context=runtime_context))
        return graph.invoke(payload, config=config, context=runtime_context)

    result = invoke("Prepare a report", context)
    assert result["todos"] == [completed]
    marker = "<\u672c\u8f6e\u4efb\u52a1\u89c4\u5212>"
    assert len(model.observed_systems) == 3
    assert all(marker in prompt for prompt in model.observed_systems)
    invoke("Hello", context.model_copy(update={"planning_enabled": False}))
    assert marker not in model.observed_systems[-1]
    assert "write_todos" in model.observed_systems[-1]
