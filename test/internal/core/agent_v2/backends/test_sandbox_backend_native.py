"""官方 DeepAgents Sandbox 生命周期适配测试。"""

from types import SimpleNamespace

from deepagents.backends.protocol import FileUploadResponse

from internal.core.agent_v2.backends.sandbox import (
    ManagedLangSmithSandbox,
    build_sandbox_backend,
)


class _Workspace:
    def __init__(self):
        self.closed = False
        self.uploaded = []

    def ls(self, path):
        return SimpleNamespace(entries=[], error=None)

    def download_files(self, paths):
        return []

    def upload_files(self, files):
        self.uploaded.extend(files)
        return [FileUploadResponse(path=path) for path, _ in files]

    def close(self):
        self.closed = True


class _Client:
    def __init__(self):
        self.created = []
        self.deleted = []

    def create_sandbox(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(name="sandbox-1")

    def delete_sandbox(self, name):
        self.deleted.append(name)


class _ManagedBackend:
    id = "sandbox-1"

    def __init__(self, *, sandbox, client, workspace, telemetry):
        self.uploaded = []

    def upload_files(self, files):
        self.uploaded.extend(files)
        return [FileUploadResponse(path=path) for path, _ in files]

    def close(self):
        raise AssertionError("creation failure cleanup is not expected here")


def test_disabled_provider_does_not_create_sandbox():
    workspace = _Workspace()

    assert (
        build_sandbox_backend(
            settings=SimpleNamespace(AGENT_SANDBOX_PROVIDER="disabled"),
            workspace=workspace,
            sandbox_name="mydify-test",
            skill_files=[],
            memory_files=[],
            context_files=[],
        )
        is None
    )
    assert workspace.closed is False


def test_langsmith_provider_uploads_skills_and_memory(monkeypatch):
    import langsmith.sandbox
    import internal.core.agent_v2.backends.sandbox as sandbox_module

    client = _Client()
    workspace = _Workspace()
    monkeypatch.setattr(langsmith.sandbox, "SandboxClient", lambda: client)
    monkeypatch.setattr(sandbox_module, "ManagedLangSmithSandbox", _ManagedBackend)

    backend = build_sandbox_backend(
        settings=SimpleNamespace(
            AGENT_SANDBOX_PROVIDER="langsmith",
            AGENT_SANDBOX_SNAPSHOT_ID="snapshot-1",
        ),
        workspace=workspace,
        sandbox_name="mydify-test",
        skill_files=[("/skills/research/SKILL.md", b"skill")],
        memory_files=[("/memories/AGENTS.md", b"memory")],
        context_files=[("/context/request.md", b"question")],
    )

    assert backend.uploaded == [
        ("/skills/research/SKILL.md", b"skill"),
        ("/memories/AGENTS.md", b"memory"),
        ("/context/request.md", b"question"),
    ]
    assert client.created == [
        {"name": "mydify-test", "snapshot_id": "snapshot-1"}
    ]


def test_managed_backend_closes_sandbox_and_workspace(monkeypatch):
    client = _Client()
    workspace = _Workspace()
    backend = ManagedLangSmithSandbox(
        sandbox=SimpleNamespace(name="sandbox-1"),
        client=client,
        workspace=workspace,
    )
    monkeypatch.setattr(backend, "_sync_workspace_from_sandbox", lambda: None)

    backend.close()
    backend.close()

    assert client.deleted == ["sandbox-1"]
    assert workspace.closed is True
