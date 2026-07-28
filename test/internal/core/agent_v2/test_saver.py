"""saver 模块单测。"""
from internal.core.agent_v2 import saver
from internal.core.agent_v2.saver import build_thread_id, _pg_uri_for_langgraph


def test_build_thread_id_main_agent():
    thread_id = build_thread_id(
        account_id="acct1", app_id="app1", conversation_id="cnv1"
    )
    assert thread_id == "(acct1,app1,cnv1,main)"


def test_build_thread_id_subagent():
    thread_id = build_thread_id(
        account_id="acct1",
        app_id="app1",
        conversation_id="cnv1",
        agent_id="researcher",
    )
    assert thread_id == "(acct1,app1,cnv1,researcher)"


def test_pg_uri_for_langgraph_strips_psycopg_prefix():
    """SQLAlchemy 的 postgresql+psycopg:// → postgresql://。"""
    assert _pg_uri_for_langgraph(
        "postgresql+psycopg://user:pass@host/db"
    ) == "postgresql://user:pass@host/db"
    assert _pg_uri_for_langgraph(
        "postgresql+psycopg2://user:pass@host/db"
    ) == "postgresql://user:pass@host/db"


def test_pg_uri_for_langgraph_passthrough():
    """已经是 postgresql:// 直接返回。"""
    assert (
        _pg_uri_for_langgraph("postgresql://user:pass@host/db")
        == "postgresql://user:pass@host/db"
    )


def test_get_postgres_saver_uses_long_lived_connection_pool(monkeypatch):
    created = {}

    class FakePool:
        def __init__(self, **kwargs):
            created["pool_kwargs"] = kwargs
            self.closed = False

        def close(self):
            self.closed = True

    class FakeSaver:
        def __init__(self, pool):
            created["pool"] = pool
            self.setup_calls = 0

        def setup(self):
            self.setup_calls += 1

    class FakeConfig:
        SQLALCHEMY_DATABASE_URI = (
            "postgresql+psycopg://user:pass@database/mydify"
        )

    monkeypatch.setattr(saver, "_saver_singleton", None)
    monkeypatch.setattr(saver, "_saver_pool", None)
    monkeypatch.setattr(saver, "_close_registered", False)
    monkeypatch.setattr(saver, "ConnectionPool", FakePool)
    monkeypatch.setattr(saver, "PostgresSaver", FakeSaver)
    monkeypatch.setattr(saver, "Config", FakeConfig)
    monkeypatch.setattr(
        saver.atexit,
        "register",
        lambda fn: created.setdefault("registered", fn),
    )

    first = saver.get_postgres_saver()
    second = saver.get_postgres_saver()

    assert first is second
    assert first.setup_calls == 1
    assert created["pool_kwargs"]["conninfo"] == (
        "postgresql://user:pass@database/mydify"
    )
    assert created["pool_kwargs"]["kwargs"]["autocommit"] is True
    assert created["registered"] is saver.close_postgres_saver

    pool = created["pool"]
    saver.close_postgres_saver()
    assert pool.closed is True
