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

from internal.core.agent_v2.workspace_backend import (
    MyDifyWorkspaceBackend,
    WorkspacePathError,
    _normalize,
    _validate_workspace_path,
    _scope_to_conversation_id,
    build_workspace_backend,
)
from internal.model.workspace_file import WorkspaceFile


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
    with pytest.raises(WorkspacePathError, match="outside workspace scope"):
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


def test_read_nonexistent_returns_error(backend, ids):
    path = f"/workspace/{ids['account_id']}/{ids['app_id']}/seed/missing.txt"
    result = backend.read(path)
    assert result.error is not None
    assert "file not found" in result.error


def test_read_rejects_path_outside_scope(backend):
    result = backend.read("/etc/passwd")
    assert result.error is not None
    assert "outside workspace scope" in result.error


def test_write_rejects_dot_segment(backend, ids):
    path = f"/workspace/{ids['account_id']}/{ids['app_id']}/{ids['conversation_id']}/../escape"
    result = backend.write(path, "evil")
    assert result.error is not None
    assert "illegal segment" in result.error


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
    assert f"{base}/seed/a.md" in paths
    assert f"{base}/seed/b.md" in paths


def test_glob_matches_pattern(backend, ids):
    base = f"/workspace/{ids['account_id']}/{ids['app_id']}"
    backend.write(f"{base}/seed/notes.md", "")
    backend.write(f"{base}/seed/data.csv", "")
    result = backend.glob("*.md", f"{base}/seed")
    assert result.error is None
    matched = [e["path"] for e in result.matches]
    assert f"{base}/seed/notes.md" in matched
    assert all("data.csv" not in p for p in matched)


def test_grep_finds_matching_lines(backend, ids):
    base = f"/workspace/{ids['account_id']}/{ids['app_id']}"
    backend.write(f"{base}/seed/log.txt", "INFO start\nERROR fail\nINFO end")
    result = backend.grep("ERROR", f"{base}/seed")
    assert result.error is None
    assert len(result.matches) == 1
    assert result.matches[0]["text"] == "ERROR fail"


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


# ---------- factory ----------


def test_build_workspace_backend_returns_instance(session, ids):
    backend = build_workspace_backend(session, **ids)
    assert isinstance(backend, MyDifyWorkspaceBackend)
    assert backend._session is session
    assert backend._account_id == ids["account_id"]
    assert backend._app_id == ids["app_id"]
    assert backend._conversation_id == ids["conversation_id"]


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