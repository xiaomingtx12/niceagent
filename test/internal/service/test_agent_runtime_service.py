"""Unified DeepAgents runtime assembly tests."""

from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock
from uuid import uuid4

import pytest
from deepagents.backends.protocol import SandboxBackendProtocol
from sqlalchemy import create_engine, event

from config.config import Config
from internal.core.agent_v2.backends.deny import DenyBackend
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

    resources = service.prepare(
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

    assert resources.checkpointer is checkpointer
    assert resources.skill_sources == ["/skills/"]
    assert resources.memory_sources == ["/memories/AGENTS.md"]
    assert resources.context_sources == ["/context/request.md"]
    assert resources.permissions[-1].mode == "deny"
    assert isinstance(resources.backend.default, DenyBackend)
    assert not isinstance(
        resources.backend.default,
        SandboxBackendProtocol,
    )
    assert all(
        not isinstance(backend, SandboxBackendProtocol)
        for backend in resources.backend.routes.values()
    )
    service.skill_service.list_enabled_latest.assert_called_once_with(
        account_id=ANY,
        app_id=ANY,
        bindings=[{"name": "research"}],
    )
    service.memory_fact_service.recall_top_k.assert_called_once()
    assert resources.subagent_skill_sources == {
        "researcher": ["/subagent_skills/researcher/"]
    }
    assert "Research carefully" in resources.backend.read(
        "/skills/research/SKILL.md"
    ).file_data["content"]
    assert "Research carefully" in resources.backend.read(
        "/subagent_skills/researcher/research/SKILL.md"
    ).file_data["content"]
    assert "用户偏好中文" in resources.backend.read(
        "/memories/AGENTS.md"
    ).file_data["content"]
    assert resources.backend.read("/unknown.txt").error is not None
    runtime_workspace = resources.backend.routes["/workspace/"]
    runtime_workspace.close()
    engine.dispose()


def test_prepare_rejects_subagent_skill_outside_enabled_app_catalog():
    from internal.core.agent_v2.subagents.specs import SubAgentSpecError

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


def test_prepare_closes_runtime_session_when_assembly_fails(monkeypatch):
    """运行时装配失败时关闭独立数据库 Session。"""
    import internal.service.agent_runtime_service as runtime_module

    runtime_session = MagicMock()
    service = _runtime_service(SimpleNamespace())
    service.skill_service.list_enabled_latest.return_value = []
    monkeypatch.setattr(
        runtime_module,
        "sessionmaker",
        lambda **_kwargs: lambda: runtime_session,
    )
    monkeypatch.setattr(
        service,
        "_assemble_resources",
        MagicMock(side_effect=RuntimeError("装配失败")),
    )

    with pytest.raises(RuntimeError, match="装配失败"):
        service.prepare(
            account_id=uuid4(),
            app_id=uuid4(),
            conversation_id=uuid4(),
            created_by=uuid4(),
            end_user_id=None,
            query="question",
        )

    runtime_session.close.assert_called_once_with()


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


def test_all_agent_entrypoints_use_the_runtime_resources_contract():
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
        "backend=runtime.backend",
        "store=runtime.store",
        "permissions=runtime.permissions",
        "skill_sources=runtime.skill_sources",
        "memory_sources=runtime.memory_sources",
        "context_sources=runtime.context_sources",
        "subagent_skill_sources=runtime.subagent_skill_sources",
        "telemetry=runtime.telemetry",
        "skill_bindings=",
        "subagents=",
    ]

    for path in entrypoints:
        source = path.read_text(encoding="utf-8")
        missing = [token for token in required_tokens if token not in source]
        assert not missing, f"{path.name} missing runtime tokens: {missing}"


def test_internal_code_has_no_legacy_agent_imports():
    """All executable imports resolve through the consolidated Agent V2 tree."""
    root = Path(__file__).resolve().parents[3] / "internal"
    legacy_import = re.compile(
        r"(?:from\s+internal\.core\.agent\s+import|"
        r"import\s+internal\.core\.agent(?:\s|$))"
    )

    matches = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if legacy_import.search(path.read_text(encoding="utf-8"))
    ]

    assert matches == []


def test_filesystem_permissions_end_with_global_deny():
    """Unknown paths and private namespaces remain fail-closed."""
    from internal.service.agent_runtime_service import build_filesystem_permissions

    permissions = build_filesystem_permissions()

    assert permissions[-1].mode == "deny"
    assert permissions[-1].paths == ["/**"]
    assert set(permissions[-1].operations) == {"read", "write"}
    assert "/workspace/.audit/**" in permissions[0].paths
    assert "/workspace/.system/**" in permissions[0].paths


@pytest.mark.parametrize(
    ("key", "value"),
    [("WORKSPACE_MAX_BYTES", "0"), ("WORKSPACE_MAX_FILES", "-1")],
)
def test_invalid_workspace_security_config_fails_fast(monkeypatch, key, value):
    """Disabled Workspace limits cannot silently enter runtime config."""
    monkeypatch.setenv(key, value)

    with pytest.raises(ValueError, match="Workspace limits"):
        Config()


def test_production_requires_explicit_workspace_limits(monkeypatch):
    """Production startup cannot inherit development quota defaults."""
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.delenv("WORKSPACE_MAX_BYTES", raising=False)
    monkeypatch.delenv("WORKSPACE_MAX_FILES", raising=False)

    with pytest.raises(ValueError, match="required in production"):
        Config()


def test_invalid_sandbox_provider_fails_fast(monkeypatch):
    """Unknown Sandbox providers cannot enter runtime configuration."""
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("WORKSPACE_MAX_BYTES", "1024")
    monkeypatch.setenv("WORKSPACE_MAX_FILES", "10")
    monkeypatch.setenv("AGENT_SANDBOX_PROVIDER", "bubblewrap")

    with pytest.raises(ValueError, match="AGENT_SANDBOX_PROVIDER"):
        Config()


def test_invalid_memory_backend_fails_fast(monkeypatch):
    """Unknown memory stores cannot enter runtime configuration."""
    monkeypatch.setenv("AGENT_MEMORY_BACKEND", "sqlite")

    with pytest.raises(ValueError, match="AGENT_MEMORY_BACKEND"):
        Config()
