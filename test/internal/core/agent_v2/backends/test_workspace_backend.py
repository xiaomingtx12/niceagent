"""MyDifyWorkspaceBackend 单测（SQLite in-memory, 不依赖 DB 服务）。

覆盖:
- 路径白名单（跨租户、跨 app、跨 conversation、.. 遍历、. 段）
- read / write / edit 端到端 (DB 实际行)
- ls / glob / grep 列表
- upload_files / download_files 批量
- 软版本 version+1
- conversation_id 自动映射 (seed → NULL, cnv → UUID)
"""
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from internal.core.agent_v2.backends.workspace import (
    MyDifyWorkspaceBackend,
)
from internal.core.agent_v2.backends.workspace_path import (
    WorkspacePathError,
    normalize_workspace_path as _normalize,
    scope_to_conversation_id as _scope_to_conversation_id,
    validate_workspace_path as _validate_workspace_path,
)
from internal.core.agent_v2.backends.workspace_quota import WorkspaceLimits
from internal.core.agent_v2.runtime.telemetry import (
    AgentTelemetry,
    AgentTelemetryEvent,
)
from internal.model.workspace_file import WorkspaceFile


class _RecordingSink:
    """保存测试观察到的遥测事件。"""

    def __init__(self):
        self.events = []

    def record(self, event: AgentTelemetryEvent) -> None:
        """记录一个事件。"""
        self.events.append(event)


@pytest.fixture()
def session():
    """SQLite in-memory + 自定义 uuid_generate_v4 (PG 兼容函数)."""
    import uuid as _uuid
    from sqlalchemy import event
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _register(dbapi_conn, _record):
        dbapi_conn.create_function(
            "uuid_generate_v4", 0, lambda: str(_uuid.uuid4())
        )

    WorkspaceFile.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def ids():
    return {
        "account_id": uuid4(),
        "app_id": uuid4(),
        "conversation_id": uuid4(),
    }


@pytest.fixture()
def backend(session, ids):
    return MyDifyWorkspaceBackend(
        session=session,
        account_id=ids["account_id"],
        app_id=ids["app_id"],
        conversation_id=ids["conversation_id"],
    )


# ---------- 路径白名单 ----------


def test_normalize_strips_trailing_slash():
    assert _normalize("/a/b/") == "/a/b"
    assert _normalize("/") == "/"
    assert _normalize("") == "/"


def test_validate_accepts_seed_path(ids):
    p = f"/workspace/{ids['account_id']}/{ids['app_id']}/seed/readme.md"
    assert _validate_workspace_path(p, **ids) == "seed/readme.md"


def test_validate_accepts_conversation_path(ids):
    p = f"/workspace/{ids['account_id']}/{ids['app_id']}/{ids['conversation_id']}/report.xlsx"
    assert _validate_workspace_path(
        p, **ids
    ) == f"{ids['conversation_id']}/report.xlsx"


def test_validate_rejects_other_account(ids):
    other = uuid4()
    p = f"/workspace/{other}/{ids['app_id']}/seed/x"
    with pytest.raises(WorkspacePathError, match="outside workspace scope"):
        _validate_workspace_path(p, **ids)


def test_validate_rejects_other_app(ids):
    other = uuid4()
    p = f"/workspace/{ids['account_id']}/{other}/seed/x"
    with pytest.raises(WorkspacePathError, match="outside workspace scope"):
        _validate_workspace_path(p, **ids)


def test_validate_rejects_other_conversation(ids):
    other = uuid4()
    p = f"/workspace/{ids['account_id']}/{ids['app_id']}/{other}/x"
    with pytest.raises(WorkspacePathError, match="scope"):
        _validate_workspace_path(p, **ids)


def test_validate_rejects_dot_segment(ids):
    p = f"/workspace/{ids['account_id']}/{ids['app_id']}/{ids['conversation_id']}/./x"
    with pytest.raises(WorkspacePathError, match="illegal segment"):
        _validate_workspace_path(p, **ids)


def test_validate_rejects_parent_traversal(ids):
    p = f"/workspace/{ids['account_id']}/{ids['app_id']}/{ids['conversation_id']}/../escape"
    with pytest.raises(WorkspacePathError, match="illegal segment"):
        _validate_workspace_path(p, **ids)


def test_validate_rejects_missing_scope(ids):
    """trailing-slash 后只剩 prefix → 视作 outside scope (不允许 ls root)。"""
    p = f"/workspace/{ids['account_id']}/{ids['app_id']}/"
    with pytest.raises(WorkspacePathError, match="missing scope segment"):
        _validate_workspace_path(p, **ids)


def test_scope_to_conversation_id_seed_returns_none(ids):
    assert _scope_to_conversation_id("seed/readme.md", ids["conversation_id"]) is None


def test_scope_to_conversation_id_cnv_returns_uuid(ids):
    rest = f"{ids['conversation_id']}/report.xlsx"
    assert _scope_to_conversation_id(rest, ids["conversation_id"]) == ids["conversation_id"]


# ---------- read/write 端到端 ----------


def test_write_then_read_roundtrip(backend, ids):
    path = f"/workspace/{ids['account_id']}/{ids['app_id']}/seed/note.txt"
    write_result = backend.write(path, "hello world")
    assert write_result.error is None
    assert write_result.path == path

    read_result = backend.read(path)
    assert read_result.error is None
    assert read_result.file_data["content"] == "hello world"
    assert read_result.file_data["encoding"] == "utf-8"


def test_write_creates_seed_row_with_null_conversation_id(backend, session, ids):
    path = f"/workspace/{ids['account_id']}/{ids['app_id']}/seed/readme.md"
    backend.write(path, "seed content")
    row = session.query(WorkspaceFile).one()
    assert row.conversation_id is None
    assert row.path == "seed/readme.md"
    assert row.version == 1
    assert row.storage_backend == "db"
    assert len(row.content_sha256) == 64
    assert row.is_deleted is False


def test_write_creates_cnv_row_with_conversation_id(backend, session, ids):
    path = f"/workspace/{ids['account_id']}/{ids['app_id']}/{ids['conversation_id']}/report.xlsx"
    backend.write(path, "xlsx bytes here")
    row = session.query(WorkspaceFile).one()
    assert row.conversation_id == ids["conversation_id"]
    assert row.version == 1


def test_write_increments_version_on_same_path(backend, session, ids):
    path = f"/workspace/{ids['account_id']}/{ids['app_id']}/seed/note.txt"
    backend.write(path, "v1")
    backend.write(path, "v2")
    backend.write(path, "v3")
    rows = (
        session.query(WorkspaceFile)
        .order_by(WorkspaceFile.version)
        .all()
    )
    assert [r.version for r in rows] == [1, 2, 3]
    # read 取 version 最大
    result = backend.read(path)
    assert result.file_data["content"] == "v3"


def test_latest_deleted_version_hides_older_content(backend, session, ids):
    path = "/conversation_history/thread.md"
    backend.write(path, "old")
    latest = session.query(WorkspaceFile).one()
    tombstone = WorkspaceFile(
        account_id=ids["account_id"],
        app_id=ids["app_id"],
        conversation_id=ids["conversation_id"],
        path=latest.path,
        version=2,
        storage_backend="db",
        mime_type="text/plain",
        size_bytes=0,
        content_sha256="",
        content=None,
        is_deleted=True,
    )
    session.add(tombstone)
    session.commit()

    assert backend.read(path).error is not None
    assert backend.ls("/").entries == []

    backend.write(path, "restored")
    assert backend.read(path).file_data["content"] == "restored"
    assert (
        session.query(WorkspaceFile)
        .order_by(WorkspaceFile.version.desc())
        .first()
        .version
        == 3
    )


def test_read_nonexistent_returns_error(backend, ids):
    path = f"/workspace/{ids['account_id']}/{ids['app_id']}/seed/missing.txt"
    result = backend.read(path)
    assert result.error is not None
    assert "file not found" in result.error


def test_virtual_absolute_path_outside_namespace_is_rejected(backend):
    result = backend.read("/etc/passwd")
    assert result.error is not None
    assert "not in a writable namespace" in result.error
    write = backend.write("/etc/passwd", "virtual, not host")
    assert write.error is not None


def test_write_rejects_dot_segment(backend, ids):
    path = f"/workspace/{ids['account_id']}/{ids['app_id']}/{ids['conversation_id']}/../escape"
    result = backend.write(path, "evil")
    assert result.error is not None
    assert "illegal segment" in result.error


@pytest.mark.parametrize(
    "path",
    ["/workspace/.audit/log.json", "/workspace/.system/state.json"],
)
def test_reserved_namespaces_are_hidden(backend, path):
    """Model-facing workspace paths cannot enter private namespaces."""
    assert "reserved namespace" in backend.write(path, "secret").error
    assert "reserved namespace" in backend.read(path).error
    assert backend.ls(path).error


def test_workspace_quota_allows_replacement_but_rejects_growth(session, ids):
    """Quota projections account for replacement rather than old versions."""
    sink = _RecordingSink()
    limited = MyDifyWorkspaceBackend(
        session=session,
        **ids,
        limits=WorkspaceLimits(max_bytes=5, max_files=1),
        telemetry=AgentTelemetry(sink),
    )

    assert limited.write("/workspace/a.txt", "12345").error is None
    assert limited.write("/workspace/a.txt", "1234").error is None
    assert "file quota exceeded" in limited.write(
        "/workspace/b.txt", "x"
    ).error
    assert "byte quota exceeded" in limited.write(
        "/workspace/a.txt", "123456"
    ).error
    assert limited.read("/workspace/a.txt").file_data["content"] == "1234"
    assert [event.name for event in sink.events] == [
        "workspace.write",
        "workspace.write",
        "workspace.quota_rejected",
        "workspace.quota_rejected",
    ]


# ---------- edit ----------


def test_edit_replaces_single_occurrence(backend, ids):
    path = f"/workspace/{ids['account_id']}/{ids['app_id']}/seed/note.txt"
    backend.write(path, "hello world")
    result = backend.edit(path, "world", "planet")
    assert result.error is None
    assert result.occurrences == 1
    assert backend.read(path).file_data["content"] == "hello planet"


def test_edit_with_replace_all(backend, ids):
    path = f"/workspace/{ids['account_id']}/{ids['app_id']}/seed/note.txt"
    backend.write(path, "foo bar foo")
    result = backend.edit(path, "foo", "baz", replace_all=True)
    assert result.error is None
    assert backend.read(path).file_data["content"] == "baz bar baz"


def test_edit_multiple_occurrences_without_replace_all_returns_error(backend, ids):
    path = f"/workspace/{ids['account_id']}/{ids['app_id']}/seed/note.txt"
    backend.write(path, "foo foo")
    result = backend.edit(path, "foo", "bar")
    assert result.error is not None
    assert "appears" in result.error


def test_edit_missing_old_string_returns_error(backend, ids):
    path = f"/workspace/{ids['account_id']}/{ids['app_id']}/seed/note.txt"
    backend.write(path, "hello")
    result = backend.edit(path, "world", "planet")
    assert result.error is not None
    assert "not found" in result.error


# ---------- ls / glob / grep ----------


def test_ls_returns_written_files(backend, ids):
    base = f"/workspace/{ids['account_id']}/{ids['app_id']}"
    backend.write(f"{base}/seed/a.md", "A")
    backend.write(f"{base}/seed/b.md", "B")
    result = backend.ls(f"{base}/seed")
    assert result.error is None
    paths = sorted(e["path"] for e in result.entries)
    assert paths == ["/seed/a.md", "/seed/b.md"]


def test_workspace_namespace_maps_to_current_conversation(backend, session, ids):
    backend.write("/workspace/report.md", "report")
    row = session.query(WorkspaceFile).one()
    assert row.path == f"{ids['conversation_id']}/report.md"
    assert backend.read("/workspace/report.md").file_data["content"] == "report"
    assert backend.ls("/workspace").entries[0]["path"] == "/workspace/report.md"


def test_artifacts_and_history_namespaces_are_scoped(backend, session, ids):
    backend.write("/artifacts/out.txt", "artifact")
    backend.write("/conversation_history/thread.md", "summary")
    paths = sorted(row.path for row in session.query(WorkspaceFile).all())
    assert paths == [
        f"{ids['conversation_id']}/artifacts/out.txt",
        f"{ids['conversation_id']}/conversation_history/thread.md",
    ]
    assert (
        backend.read("/conversation_history/thread.md").file_data["content"]
        == "summary"
    )


def test_glob_matches_pattern(backend, ids):
    base = f"/workspace/{ids['account_id']}/{ids['app_id']}"
    backend.write(f"{base}/seed/notes.md", "")
    backend.write(f"{base}/seed/data.csv", "")
    result = backend.glob("*.md", f"{base}/seed")
    assert result.error is None
    matched = [e["path"] for e in result.matches]
    assert "/seed/notes.md" in matched
    assert all("data.csv" not in p for p in matched)


def test_grep_finds_matching_lines(backend, ids):
    base = f"/workspace/{ids['account_id']}/{ids['app_id']}"
    backend.write(f"{base}/seed/log.txt", "INFO start\nERROR fail\nINFO end")
    result = backend.grep("ERROR", f"{base}/seed")
    assert result.error is None
    assert len(result.matches) == 1
    assert result.matches[0]["text"] == "ERROR fail"


def test_grep_honors_max_count(backend, ids):
    base = f"/workspace/{ids['account_id']}/{ids['app_id']}"
    backend.write(f"{base}/seed/log.txt", "hit one\nhit two")
    result = backend.grep("hit", f"{base}/seed", max_count=1)
    assert len(result.matches) == 1


def test_root_queries_do_not_leak_other_conversations(session, ids):
    other_conversation = uuid4()
    current = MyDifyWorkspaceBackend(session=session, **ids)
    other = MyDifyWorkspaceBackend(
        session=session,
        account_id=ids["account_id"],
        app_id=ids["app_id"],
        conversation_id=other_conversation,
    )
    base = f"/workspace/{ids['account_id']}/{ids['app_id']}"
    other.write(f"{base}/{other_conversation}/private.txt", "secret")

    assert current.ls("/").entries == []
    assert current.grep("secret").matches == []
    assert current.glob("*.txt", "/").matches == []


# ---------- upload / download ----------


def test_upload_files_writes_each(backend, ids):
    base = f"/workspace/{ids['account_id']}/{ids['app_id']}"
    files = [
        (f"{base}/seed/a.txt", b"alpha"),
        (f"{base}/seed/b.txt", b"beta"),
    ]
    responses = backend.upload_files(files)
    assert all(r.error is None for r in responses)
    assert backend.read(f"{base}/seed/a.txt").file_data["content"] == "alpha"
    assert backend.read(f"{base}/seed/b.txt").file_data["content"] == "beta"


def test_download_files_returns_content(backend, ids):
    base = f"/workspace/{ids['account_id']}/{ids['app_id']}"
    backend.write(f"{base}/seed/x.txt", "x")
    responses = backend.download_files([f"{base}/seed/x.txt"])
    assert len(responses) == 1
    assert responses[0].error is None
    assert responses[0].content == b"x"


def test_download_files_outside_scope_returns_error(backend):
    responses = backend.download_files(["/etc/passwd"])
    assert len(responses) == 1
    assert responses[0].error is not None


def test_async_upload_and_download_are_awaitable(backend, ids):
    import asyncio

    base = f"/workspace/{ids['account_id']}/{ids['app_id']}"
    path = f"{base}/seed/async.txt"

    async def run():
        uploaded = await backend.aupload_files([(path, b"async")])
        downloaded = await backend.adownload_files([path])
        return uploaded, downloaded

    uploaded, downloaded = asyncio.run(run())
    assert uploaded[0].error is None
    assert downloaded[0].content == b"async"


# ---------- async 包装 ----------


def test_async_read_equals_sync_read(backend, ids):
    import asyncio
    path = f"/workspace/{ids['account_id']}/{ids['app_id']}/seed/x.txt"
    backend.write(path, "sync content")
    result = asyncio.run(backend.aread(path))
    assert result.file_data["content"] == "sync content"


def test_async_write_equals_sync_write(backend, ids):
    import asyncio
    path = f"/workspace/{ids['account_id']}/{ids['app_id']}/seed/y.txt"
    result = asyncio.run(backend.awrite(path, "async content"))
    assert result.error is None
    assert backend.read(path).file_data["content"] == "async content"


# ---------- 多租户隔离 ----------


def test_backend_a_cannot_see_backend_b_files(session, ids):
    base = f"/workspace/{ids['account_id']}/{ids['app_id']}"
    backend_a = MyDifyWorkspaceBackend(
        session=session,
        account_id=ids["account_id"],
        app_id=ids["app_id"],
        conversation_id=ids["conversation_id"],
    )
    other_account = uuid4()
    backend_b = MyDifyWorkspaceBackend(
        session=session,
        account_id=other_account,
        app_id=ids["app_id"],
        conversation_id=ids["conversation_id"],
    )
    backend_a.write(f"{base}/seed/private.txt", "A's data")

    # backend_b 走相同 path, 但 account_id 不同 → 走 _validate 校验过不去
    # (因为 path 包含 account_a 的 UUID); 要测"跨账号"得用 backend_b 自己
    # 的合法 path 看是否能看到 backend_a 的内容。重新构造 path:
    cross_path = f"/workspace/{other_account}/{ids['app_id']}/seed/private.txt"
    result = backend_b.read(cross_path)
    assert result.error is not None
    assert "file not found" in result.error
