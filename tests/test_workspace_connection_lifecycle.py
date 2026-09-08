"""使用真实 SQLAlchemy 小连接池复现多个存活会话耗尽连接的场景。"""

from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.entity.workspace_quota import AgentWorkspaceLimits
from internal.core.agent_v2.observability.telemetry import NOOP_AGENT_TELEMETRY
from internal.model import WorkspaceFile
from internal.repository.agent_workspace_backend import AgentWorkspaceBackend
from internal.repository.workspace_file_repository import WorkspaceFileRepository
from internal.repository.workspace_file_store import WorkspaceFileStore


@pytest.fixture
def workspace_engine(tmp_path):
    """仅在临时 SQLite 文件创建测试表，不访问项目 PostgreSQL。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'workspace.db'}",
        pool_size=2, max_overflow=0, pool_timeout=0.05,
    )
    WorkspaceFile.__table__.create(engine)
    yield engine
    engine.dispose()


def make_backend(engine, file_store=None):
    """各个会话使用独立 Session，共享应用的实际连接池。"""
    return AgentWorkspaceBackend.create(
        AgentRunContext(account_id=uuid4(), app_id=uuid4(), conversation_id=uuid4(),
                        created_by=uuid4(), principal_id=uuid4()),
        NOOP_AGENT_TELEMETRY,
        engine=engine, file_store=file_store or Mock(),
        limits=AgentWorkspaceLimits(max_bytes=1000, max_files=10),
    )


@pytest.mark.parametrize("operation", ["ls", "read", "export_snapshot"])
def test_idle_conversations_do_not_exhaust_pool(workspace_engine, operation):
    """文件操作完成后让 Agent 保持存活，后续会话仍应能借到连接。"""
    backends = []
    try:
        for _ in range(8):
            backend = make_backend(workspace_engine)
            backends.append(backend)
            if operation == "read":
                backend.read("/workspace/missing.txt")
            else:
                getattr(backend, operation)()
        assert workspace_engine.pool.checkedout() == 0
    finally:
        for backend in backends:
            backend.close()


def test_file_io_and_revisions_work_after_read_session_is_released(workspace_engine):
    """实际读改写与快照使用已分离元数据，COS 调用期间不得占用数据库连接。"""
    objects = {}

    def put_bytes(key, content, **_kwargs):
        """上传可能很慢，进入上传前必须结束元数据读事务。"""
        assert workspace_engine.pool.checkedout() == 0
        objects[key] = content

    def get_bytes(key, **_kwargs):
        """内容下载也必须在数据库连接归还后进行。"""
        assert workspace_engine.pool.checkedout() == 0
        return objects[key]

    storage = Mock()
    storage.put_bytes.side_effect = put_bytes
    storage.get_bytes.side_effect = get_bytes
    backend = make_backend(workspace_engine, WorkspaceFileStore(object_storage=storage))
    try:
        assert backend.write("/workspace/a.txt", "hello").error is None
        assert backend.write("/workspace/a.txt", "hello").error is None
        assert backend.read("/workspace/a.txt").file_data["content"] == "hello"
        assert backend.edit("/workspace/a.txt", "hello", "world").error is None
        manifest, files = backend.export_snapshot()
        assert manifest["/workspace/a.txt"].revision == 2
        assert files == [("/workspace/a.txt", b"world")]
        assert backend.delete_files(["/workspace/a.txt"], expected_revisions={"/workspace/a.txt": 1})
        assert backend.delete_files(["/workspace/a.txt"], expected_revisions={"/workspace/a.txt": 2}) == []
        assert backend.ls().entries == []
        assert workspace_engine.pool.checkedout() == 0
    finally:
        backend.close()


def test_failed_read_returns_connection_and_next_read_recovers(workspace_engine):
    """借到连接后查询异常，也不能让失败会话继续占用池。"""
    backend = make_backend(workspace_engine)

    def fail_query(*_args):
        """在真正 checkout 后触发异常，验证清理路径。"""
        raise RuntimeError("query failed")

    try:
        event.listen(workspace_engine, "before_cursor_execute", fail_query)
        try:
            with pytest.raises(RuntimeError, match="query failed"):
                backend.ls()
        finally:
            event.remove(workspace_engine, "before_cursor_execute", fail_query)
        assert workspace_engine.pool.checkedout() == 0
        assert backend.ls().entries == []
        assert workspace_engine.pool.checkedout() == 0
    finally:
        backend.close()


def test_borrowed_http_session_keeps_callers_transaction(workspace_engine):
    """共享仓储不得关闭 HTTP 借入的 Session 或回滚调用方尚未提交的数据。"""
    with Session(workspace_engine) as session:
        repository = WorkspaceFileRepository(
            session, uuid4(), uuid4(), uuid4(),
            AgentWorkspaceLimits(max_bytes=1000, max_files=10),
        )
        session.execute(select(WorkspaceFile))
        transaction = session.get_transaction()
        assert repository.list() == []
        assert repository.fetch("missing") is None
        assert session.get_transaction() is transaction
        assert transaction.is_active
