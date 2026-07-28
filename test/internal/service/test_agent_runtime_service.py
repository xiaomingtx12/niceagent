"""Unified DeepAgents runtime assembly tests."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event

from internal.model import WorkspaceFile
from internal.service.agent_runtime_service import AgentRuntimeService


def _runtime_service(engine):
    service = AgentRuntimeService.__new__(AgentRuntimeService)
    service.db = SimpleNamespace(engine=engine)
    service.skill_service = MagicMock()
    service.memory_fact_service = MagicMock()
    return service


def test_prepare_assembles_workspace_facts_and_scoped_subagent_skills(
    monkeypatch,
):
    import uuid as uuid_module
    import internal.service.agent_runtime_service as runtime_module

    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _register(dbapi_conn, _record):
        dbapi_conn.create_function(
            "uuid_generate_v4",
            0,
            lambda: str(uuid_module.uuid4()),
        )

    WorkspaceFile.__table__.create(engine)
    service = _runtime_service(engine)
    service.skill_service.list_enabled_latest.return_value = [
        SimpleNamespace(
            name="research",
            enabled=True,
            skill_md=(
                "---\n"
                "name: research\n"
                "description: research skill\n"
                "---\n\n"
                "Research carefully."
            ),
        )
    ]
    fact = SimpleNamespace(fact_type="preference", content="用户偏好中文")
    service.memory_fact_service.recall_top_k.return_value = [fact]
    checkpointer = object()
    monkeypatch.setattr(
        runtime_module,
        "get_configured_checkpointer",
        lambda: checkpointer,
    )

    bundle = service.prepare(
        account_id=uuid4(),
        app_id=uuid4(),
        conversation_id=uuid4(),
        created_by=uuid4(),
        end_user_id=uuid4(),
        query="current question",
        skill_bindings=[{"name": "research"}],
        subagents=[
            {
                "name": "researcher",
                "description": "research",
                "system_prompt": "work",
                "skills": ["research"],
            },
        ],
    )

    assert bundle.checkpointer is checkpointer
    assert bundle.skill_sources == ["/skills/"]
    assert bundle.memory_sources == ["/memories/profile.md"]
    assert bundle.permissions[-1].mode == "deny"
    service.skill_service.list_enabled_latest.assert_called_once_with(
        account_id=ANY,
        app_id=ANY,
        bindings=[{"name": "research"}],
    )
    service.memory_fact_service.recall_top_k.assert_called_once()
    assert "用户偏好中文" in bundle.workspace_backend.read(
        "/memories/profile.md"
    ).file_data["content"]
    assert bundle.subagent_skill_sources == {
        "researcher": ["/subagent_skills/researcher/"]
    }
    assert "Research carefully" in bundle.workspace_backend.read(
        "/skills/research/SKILL.md"
    ).file_data["content"]
    assert "Research carefully" in bundle.workspace_backend.read(
        "/subagent_skills/researcher/research/SKILL.md"
    ).file_data["content"]
    assert bundle.workspace_backend.read("/unknown.txt").error is not None
    runtime_workspace = bundle.workspace_backend.routes["/workspace/"]
    runtime_workspace.close()
    engine.dispose()


def test_prepare_rejects_subagent_skill_outside_enabled_app_catalog():
    from internal.core.agent_v2.subagent_specs import SubAgentSpecError

    engine = create_engine("sqlite:///:memory:")
    WorkspaceFile.__table__.create(engine)
    service = _runtime_service(engine)
    service.skill_service.list_enabled_latest.return_value = []
    service.memory_fact_service.recall_top_k.return_value = []

    with pytest.raises(SubAgentSpecError, match="未启用的 Skills"):
        service.prepare(
            account_id=uuid4(),
            app_id=uuid4(),
            conversation_id=uuid4(),
            created_by=uuid4(),
            end_user_id=None,
            query="question",
            subagents=[
                {
                    "name": "researcher",
                    "description": "research",
                    "system_prompt": "work",
                    "skills": ["missing"],
                }
            ],
        )
    engine.dispose()


def test_extract_and_store_facts_accepts_structured_dict_and_creator_scope(
    monkeypatch,
):
    import internal.service.agent_runtime_service as runtime_module

    service = AgentRuntimeService.__new__(AgentRuntimeService)
    service.db = SimpleNamespace()
    service.skill_service = MagicMock()
    service.memory_fact_service = MagicMock()
    service.memory_fact_service.create_auto_fact.return_value = object()
    extractor = MagicMock()
    extractor.invoke.return_value = {
        "facts": [
            {
                "content": "项目使用 PostgreSQL 16",
                "importance": 0.9,
                "fact_type": "project",
            },
            {"malformed": True},
        ]
    }
    llm = MagicMock()
    llm.with_structured_output.return_value = extractor
    monkeypatch.setattr(
        runtime_module,
        "Config",
        lambda: SimpleNamespace(AGENT_AUTO_EXTRACT_MEMORY=True),
    )

    stored = service.extract_and_store_facts(
        account_id=uuid4(),
        app_id=uuid4(),
        end_user_id=None,
        conversation_id=uuid4(),
        message_id=uuid4(),
        query="我们项目使用 PostgreSQL 16",
        answer="已记录",
        llm=llm,
    )

    assert stored == 1
    call = service.memory_fact_service.create_auto_fact.call_args.kwargs
    assert call["end_user_id"] is None
    assert call["content"] == "项目使用 PostgreSQL 16"
    assert call["importance"] == 0.9
    assert "embedding" not in call


def test_all_agent_entrypoints_use_the_runtime_bundle_contract():
    root = Path(__file__).resolve().parents[3]
    entrypoints = [
        root / "internal" / "service" / "app_service.py",
        root / "internal" / "service" / "assistant_agent_service.py",
        root / "internal" / "service" / "web_app_service.py",
        root / "internal" / "service" / "openapi_service.py",
        root / "internal" / "service" / "wechat_service.py",
    ]
    required_tokens = [
        "self.agent_runtime_service.prepare(",
        "BaseAgent(",
        "checkpointer=runtime.checkpointer",
        "workspace_backend=runtime.workspace_backend",
        "permissions=runtime.permissions",
        "skill_sources=runtime.skill_sources",
        "memory_sources=runtime.memory_sources",
        "subagent_skill_sources=runtime.subagent_skill_sources",
        "skill_bindings=",
        "subagents=",
    ]

    for path in entrypoints:
        source = path.read_text(encoding="utf-8")
        missing = [token for token in required_tokens if token not in source]
        assert not missing, f"{path.name} missing runtime tokens: {missing}"
