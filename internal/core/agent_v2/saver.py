"""LangGraph Postgres checkpointer 的可选进程级生命周期管理。"""

from __future__ import annotations

import atexit
from threading import Lock
from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.memory import InMemorySaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config.config import Config


_saver_singleton: Optional[PostgresSaver] = None
_saver_pool: Optional[ConnectionPool] = None
_memory_saver_singleton: Optional[InMemorySaver] = None
_saver_lock = Lock()
_close_registered = False


def get_postgres_saver() -> PostgresSaver:
    """按需创建线程安全的进程级 PostgresSaver。

    ``PostgresSaver.from_conn_string()`` 是上下文管理器，只适合局部 ``with``
    用法，不能直接缓存。长期运行的 Flask 进程改为显式持有
    ``psycopg_pool.ConnectionPool``，并在进程退出时关闭连接池。
    """
    global _close_registered, _saver_pool, _saver_singleton
    with _saver_lock:
        if _saver_singleton is None:
            connection_string = _pg_uri_for_langgraph(Config().SQLALCHEMY_DATABASE_URI)
            _saver_pool = ConnectionPool(
                conninfo=connection_string,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
                min_size=1,
                max_size=10,
                open=True,
            )
            _saver_singleton = PostgresSaver(_saver_pool)
            _saver_singleton.setup()
            if not _close_registered:
                atexit.register(close_postgres_saver)
                _close_registered = True
        return _saver_singleton


def get_configured_checkpointer(
    mode: str | None = None,
) -> BaseCheckpointSaver | bool:
    """Resolve the supported runtime saver mode.

    Production defaults to PostgreSQL. Tests and local ephemeral runs may opt
    into ``memory``; ``disabled`` is explicit and never selected implicitly.
    """
    selected = (mode or Config().AGENT_CHECKPOINT_BACKEND).strip().lower()
    if selected == "postgres":
        return get_postgres_saver()
    if selected == "memory":
        global _memory_saver_singleton
        with _saver_lock:
            if _memory_saver_singleton is None:
                _memory_saver_singleton = InMemorySaver()
            return _memory_saver_singleton
    if selected in {"disabled", "false", "off", "none"}:
        return False
    raise ValueError(
        "AGENT_CHECKPOINT_BACKEND 必须是 postgres、memory 或 disabled"
    )


def close_postgres_saver() -> None:
    """关闭 saver 使用的连接池；可供测试和应用 shutdown hook 调用。"""
    global _saver_pool, _saver_singleton
    with _saver_lock:
        pool = _saver_pool
        _saver_singleton = None
        _saver_pool = None
        if pool is not None:
            pool.close()


def _pg_uri_for_langgraph(sqlalchemy_uri: str) -> str:
    """把 SQLAlchemy driver URI 转成 psycopg 可直接连接的 URI。"""
    if sqlalchemy_uri.startswith("postgresql+"):
        return "postgresql://" + sqlalchemy_uri.split("://", 1)[1]
    return sqlalchemy_uri


def build_thread_id(
    *,
    account_id: str,
    app_id: str,
    conversation_id: str,
    agent_id: str = "main",
) -> str:
    """构造 LangGraph thread_id。

    当前同步 DeepAgents 团队共用主图 thread；``agent_id`` 保留给未来独立部署
    的远程 Agent。LangGraph 的 thread_id 是字符串，不是 Python tuple。
    """
    return f"({account_id},{app_id},{conversation_id},{agent_id})"


__all__ = [
    "BaseCheckpointSaver",
    "PostgresSaver",
    "build_thread_id",
    "close_postgres_saver",
    "get_postgres_saver",
    "get_configured_checkpointer",
]
