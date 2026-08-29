"""LangGraph Store 的进程级生命周期管理。"""

from __future__ import annotations

import atexit
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable, Optional

from config.config import Config
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres import PostgresStore
from psycopg_pool import ConnectionPool


def _pg_uri_for_langgraph(sqlalchemy_uri: str) -> str:
    """把 SQLAlchemy driver URI 转成 psycopg 可连接的 URI。"""
    if sqlalchemy_uri.startswith("postgresql+"):
        return "postgresql://" + sqlalchemy_uri.split("://", 1)[1]
    return sqlalchemy_uri


def create_postgres_store(
    *,
    config_factory: Callable[[], Any] = Config,
    store_factory: Any = PostgresStore,
) -> tuple[PostgresStore, Any]:
    """创建并初始化一个长期存活的 PostgresStore。"""
    connection_string = _pg_uri_for_langgraph(config_factory().SQLALCHEMY_DATABASE_URI)
    context = store_factory.from_conn_string(
        connection_string,
        pool_config={
            "min_size": 1,
            "max_size": 10,
            "check": ConnectionPool.check_connection,
        },
    )
    store = context.__enter__()
    try:
        store.setup()
    except Exception:
        context.__exit__(None, None, None)
        raise
    return store, context


@dataclass
class StoreProvider:
    """管理进程级长期文件 Store。"""

    config_factory: Callable[[], Any] = Config
    store_factory: Any = PostgresStore
    register_exit: Callable[[Callable[[], None]], Any] = atexit.register
    _store: Optional[BaseStore] = field(default=None, init=False, repr=False)
    _context: Any = field(default=None, init=False, repr=False)
    _memory_store: Optional[InMemoryStore] = field(
        default=None,
        init=False,
        repr=False,
    )
    _close_registered: bool = field(default=False, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def get_postgres_store(self) -> PostgresStore:
        """按需创建并缓存 PostgresStore。"""
        with self._lock:
            if self._store is None:
                self._store, self._context = create_postgres_store(
                    config_factory=self.config_factory,
                    store_factory=self.store_factory,
                )
                if not self._close_registered:
                    self.register_exit(self.close_postgres_store)
                    self._close_registered = True
            return self._store  # type: ignore[return-value]

    def get_configured_store(self, mode: str | None = None) -> BaseStore:
        """按配置选择 PostgreSQL 或内存 Store。"""
        selected = (mode or self.config_factory().AGENT_MEMORY_BACKEND).strip().lower()
        if selected == "postgres":
            return self.get_postgres_store()
        if selected == "memory":
            with self._lock:
                if self._memory_store is None:
                    self._memory_store = InMemoryStore()
                return self._memory_store
        if selected in {"disabled", "false", "off", "none"}:
            raise ValueError(
                "AGENT_MEMORY_BACKEND 不允许关闭，必须是 postgres 或 memory"
            )
        raise ValueError("AGENT_MEMORY_BACKEND 必须是 postgres 或 memory")

    def close_postgres_store(self) -> None:
        """关闭 PostgresStore 的连接池或连接。"""
        with self._lock:
            context = self._context
            self._store = None
            self._context = None
            if context is not None:
                context.__exit__(None, None, None)


_provider = StoreProvider()


def get_configured_store(mode: str | None = None) -> BaseStore:
    """使用默认配置获取长期文件 Store。"""
    return _provider.get_configured_store(mode)


def get_postgres_store() -> PostgresStore:
    """使用默认配置获取进程级 PostgresStore。"""
    return _provider.get_postgres_store()


def close_postgres_store() -> None:
    """关闭默认 Provider 持有的 PostgresStore。"""
    _provider.close_postgres_store()


__all__ = [
    "PostgresStore",
    "StoreProvider",
    "close_postgres_store",
    "create_postgres_store",
    "get_configured_store",
    "get_postgres_store",
]
