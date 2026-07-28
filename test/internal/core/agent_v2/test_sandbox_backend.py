"""Sandbox execution, persistence, and audit tests."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from internal.core.agent_v2.sandbox_backend import MyDifySandboxBackend
from internal.model import WorkspaceFile


@pytest.fixture
def backend():
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
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    instance = MyDifySandboxBackend(
        session=session,
        account_id=uuid4(),
        app_id=uuid4(),
        conversation_id=uuid4(),
        created_by=uuid4(),
    )
    yield instance
    instance.close()
    engine.dispose()


def test_invalid_execute_is_rejected_and_audited(backend):
    response = backend.execute("")

    assert response.exit_code == 2
    audit_files = [
        item["path"]
        for item in backend.ls("/workspace").entries
        if "/workspace/.audit/execute-" in item["path"]
    ]
    assert len(audit_files) == 1
    audit = backend.read(audit_files[0]).file_data["content"]
    assert '"exit_code": 2' in audit


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows process-level fallback contract",
)
def test_windows_python_execution_persists_workspace_changes(backend):
    backend.write(
        "/workspace/run.py",
        (
            "from pathlib import Path\n"
            "Path('workspace/result.txt').write_text('done', encoding='utf-8')\n"
            "print('sandbox-ok')\n"
        ),
    )

    response = backend.execute("python workspace/run.py")

    assert response.exit_code == 0
    assert response.output == "sandbox-ok"
    assert backend.read("/workspace/result.txt").file_data["content"] == "done"


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows process-level fallback contract",
)
def test_windows_audit_policy_blocks_host_file_deletion(backend, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    backend.write(
        "/workspace/attack.py",
        f"import os\nos.remove({str(outside)!r})\n",
    )

    response = backend.execute("python workspace/attack.py")

    assert response.exit_code != 0
    assert outside.read_text(encoding="utf-8") == "keep"
    assert "PermissionError" in response.output
