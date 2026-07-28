"""Builtin Skill projection through native DeepAgents SkillsMiddleware."""

from types import SimpleNamespace
from typing import Any

from deepagents.backends import CompositeBackend, StateBackend
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from internal.core.agent_v2.factory import build_deep_agent
from internal.core.agent_v2.skill_backend import SkillCatalogBackend


class RecordingChatModel(BaseChatModel):
    calls: list[list[Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "skill-recording-model"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(self, messages, stop=None, **kwargs):
        self.calls.append(list(messages))
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="ok"))]
        )


def _skill():
    return SimpleNamespace(
        name="research",
        enabled=True,
        skill_md=(
            "---\n"
            "name: research\n"
            "description: search carefully\n"
            "---\n\n"
            "Use primary sources."
        ),
    )


def test_skill_catalog_is_read_only_and_supports_progressive_read():
    backend = SkillCatalogBackend([_skill()])

    assert backend.ls("/").entries[0]["path"] == "/research/"
    assert "Use primary sources" in backend.read(
        "/research/SKILL.md"
    ).file_data["content"]
    assert backend.write("/research/SKILL.md", "tamper").error is not None


def test_native_skills_middleware_discovers_builtin_catalog():
    catalog = SkillCatalogBackend([_skill()])
    backend = CompositeBackend(
        default=StateBackend(),
        routes={"/skills/": catalog},
        artifacts_root="/",
    )
    model = RecordingChatModel()
    graph = build_deep_agent(
        llm=model,
        tools=[],
        system_prompt="main identity",
        review_config={},
        account_id="account",
        app_id="app",
        conversation_id="conversation",
        workspace_backend=backend,
        skill_sources=["/skills/"],
        use_checkpointer=False,
    )

    graph.invoke({"messages": [HumanMessage(content="research this")]})

    system = next(
        message
        for message in model.calls[0]
        if isinstance(message, SystemMessage)
    )
    rendered = str(system.content)
    assert "research" in rendered
    assert "search carefully" in rendered
    assert "/skills/research/SKILL.md" in rendered
