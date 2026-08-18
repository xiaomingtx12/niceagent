"""Checkpoint Provider 单测。"""

import pytest

from internal.core.agent_v2.persistence.checkpoint import (
    CheckpointProvider,
    _pg_uri_for_langgraph,
    build_thread_id,
)


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
    """SQLAlchemy 的 postgresql+psycopg:// 转成 psycopg URI。"""
    assert _pg_uri_for_langgraph(
        "postgresql+psycopg://user:pass@host/db"
    ) == "postgresql://user:pass@host/db"
    assert _pg_uri_for_langgraph(
        "postgresql+psycopg2://user:pass@host/db"
    ) == "postgresql://user:pass@host/db"


def test_pg_uri_for_langgraph_passthrough():
    assert (
        _pg_uri_for_langgraph("postgresql://user:pass@host/db")
        == "postgresql://user:pass@host/db"
    )


def test_provider_uses_long_lived_connection_pool():
    created = {}
    registered = []

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
        AGENT_CHECKPOINT_BACKEND = "postgres"

    provider = CheckpointProvider(
        config_factory=FakeConfig,
        connection_pool_factory=FakePool,
        saver_factory=FakeSaver,
        register_exit=registered.append,
    )

    first = provider.get_configured_checkpointer()
    second = provider.get_configured_checkpointer()

    assert first is second
    assert first.setup_calls == 1
    assert created["pool_kwargs"]["conninfo"] == (
        "postgresql://user:pass@database/mydify"
    )
    assert created["pool_kwargs"]["kwargs"]["autocommit"] is True
    assert registered == [provider.close_postgres_saver]

    pool = created["pool"]
    provider.close_postgres_saver()
    assert pool.closed is True


def test_configured_memory_saver_is_provider_stable():
    class FakeConfig:
        AGENT_CHECKPOINT_BACKEND = "memory"

    provider = CheckpointProvider(config_factory=FakeConfig)
    first = provider.get_configured_checkpointer()
    second = provider.get_configured_checkpointer()

    assert first is second
    assert provider.get_configured_checkpointer("disabled") is False


def test_configured_saver_rejects_unknown_mode():
    provider = CheckpointProvider(
        config_factory=lambda: type(
            "FakeConfig",
            (),
            {"AGENT_CHECKPOINT_BACKEND": "memory"},
        )()
    )
    with pytest.raises(ValueError, match="postgres"):
        provider.get_configured_checkpointer("sqlite")
