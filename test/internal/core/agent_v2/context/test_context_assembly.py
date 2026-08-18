"""上下文投影器的边界测试。"""

from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from internal.core.agent_v2.context.assembler import AgentContextAssembler
from internal.core.agent_v2.contract import AgentRunContext
from internal.model import WorkspaceFile


def _settings():
    return SimpleNamespace(
        WORKSPACE_MAX_BYTES=1024 * 1024,
        WORKSPACE_MAX_FILES=10,
        AGENT_MEMORY_RECALL_K=5,
        AGENT_SANDBOX_PROVIDER="disabled",
    )


def _engine():
    import uuid as uuid_module

    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _register(dbapi_conn, _record):
        dbapi_conn.create_function(
            "uuid_generate_v4",
            0,
            lambda: str(uuid_module.uuid4()),
        )

    WorkspaceFile.__table__.create(engine)
    return engine


def test_context_assembler_separates_skill_memory_and_workspace_projections():
    engine = _engine()
    session = sessionmaker(bind=engine)()
    account_id, app_id, conversation_id = uuid4(), uuid4(), uuid4()
    skill = SimpleNamespace(
        name="research",
        enabled=True,
        package_files={"SKILL.md": "research"},
    )
    memory_service = SimpleNamespace(
        recall_top_k=lambda **_kwargs: [
            SimpleNamespace(fact_type="preference", content="中文")
        ]
    )
    assembler = AgentContextAssembler(
        memory_fact_service=memory_service,
        checkpointer_provider=lambda: "checkpoint",
    )

    plan = assembler.assemble(
        runtime_session=session,
        run_context=AgentRunContext(
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation_id,
            created_by=uuid4(),
            end_user_id=uuid4(),
            query="偏好",
        ),
        skills=[skill],
        subagents=[
            {
                "name": "researcher",
                "description": "research",
                "system_prompt": "work",
                "skills": ["research"],
            }
        ],
        settings=_settings(),
    )

    assert plan.checkpointer == "checkpoint"
    assert plan.skill_sources == ["/skills/"]
    assert plan.memory_sources == ["/memories/AGENTS.md"]
    assert plan.context_sources == ["/context/request.md"]
    assert plan.subagent_skill_sources == {
        "researcher": ["/subagent_skills/researcher/"]
    }
    assert "research" in plan.backend.read(
        "/skills/research/SKILL.md"
    ).file_data["content"]
    assert "中文" in plan.backend.read(
        "/memories/AGENTS.md"
    ).file_data["content"]
    assert "偏好" in plan.backend.read(
        "/context/request.md"
    ).file_data["content"]
    assert plan.backend.read("/workspace/missing.txt").error is not None

    plan.backend.routes["/workspace/"].close()
    engine.dispose()


def test_sandbox_mode_uses_default_backend_and_uploads_native_skill_files(
    monkeypatch,
):
    import internal.core.agent_v2.context.sandbox as sandbox_module

    engine = _engine()
    session = sessionmaker(bind=engine)()
    sandbox = object()
    captured = {}

    def build_backend(**kwargs):
        captured.update(kwargs)
        return sandbox

    monkeypatch.setattr(sandbox_module, "build_sandbox_backend", build_backend)
    assembler = AgentContextAssembler(
        memory_fact_service=SimpleNamespace(
            recall_top_k=lambda **_kwargs: []
        ),
        checkpointer_provider=lambda: "checkpoint",
    )
    settings = _settings()
    settings.AGENT_SANDBOX_PROVIDER = "langsmith"
    skill = SimpleNamespace(
        name="research",
        enabled=True,
        skill_md="skill",
        package_files={
            "SKILL.md": "skill",
            "scripts/run.py": "print('ok')",
        },
    )

    plan = assembler.assemble(
        runtime_session=session,
        run_context=AgentRunContext(
            account_id=uuid4(),
            app_id=uuid4(),
            conversation_id=uuid4(),
            created_by=uuid4(),
            end_user_id=None,
            query="question",
        ),
        skills=[skill],
        subagents=[],
        settings=settings,
    )

    assert plan.backend.default is sandbox
    assert plan.backend.routes == {}
    assert captured["skill_files"] == [
        ("/skills/research/SKILL.md", b"skill"),
        ("/skills/research/scripts/run.py", b"print('ok')"),
    ]
    assert captured["context_files"] == [
        ("/context/request.md", b"# Current request\n\nquestion\n")
    ]
    assert plan.permissions == []
    session.close()
    engine.dispose()
