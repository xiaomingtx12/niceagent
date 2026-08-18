"""Persistent thread-state continuity without DB-history duplication."""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from internal.core.agent_v2.runtime.factory import build_deep_agent, build_thread_config


class RecordingChatModel(BaseChatModel):
    calls: list[list[Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "recording-chat-model"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(self, messages, stop=None, **kwargs):
        self.calls.append(list(messages))
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(content=f"answer-{len(self.calls)}")
                )
            ]
        )


def test_same_thread_restores_prior_state_and_adds_only_current_message():
    model = RecordingChatModel()
    graph = build_deep_agent(
        llm=model,
        tools=[],
        system_prompt="stable identity",
        review_config={},
        checkpointer=InMemorySaver(),
    )
    config = build_thread_config(
        account_id="account",
        app_id="app",
        conversation_id="conversation",
    )

    graph.invoke(
        {"messages": [HumanMessage(content="first")]},
        config=config,
    )
    state = graph.invoke(
        {"messages": [HumanMessage(content="second")]},
        config=config,
    )

    second_call = [
        message.content
        for message in model.calls[1]
        if not isinstance(message, SystemMessage)
    ]
    assert second_call == ["first", "answer-1", "second"]
    assert [message.content for message in state["messages"]] == [
        "first",
        "answer-1",
        "second",
        "answer-2",
    ]
