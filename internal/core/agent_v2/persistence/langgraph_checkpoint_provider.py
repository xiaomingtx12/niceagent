"""LangGraph checkpoint 的真实持久化实现。

这个模块只负责 Saver、连接池和稳定线程标识。
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable, Optional

from config.config import Config
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def _pg_uri_for_langgraph(sqlalchemy_uri: str) -> str:
    """把 SQLAlchemy driver URI 转成 psycopg 可直接连接的 URI。"""
    if sqlalchemy_uri.startswith("postgresql+"):
        return "postgresql://" + sqlalchemy_uri.split("://", 1)[1]
    return sqlalchemy_uri


def create_postgres_saver(
    *,
    config_factory: Callable[[], Any] = Config,
    connection_pool_factory: Callable[..., Any] = ConnectionPool,
    saver_factory: Callable[..., Any] = PostgresSaver,
) -> tuple[Any, Any]:
    """创建并初始化一个长期存活的 PostgresSaver 和连接池。"""
    connection_string = _pg_uri_for_langgraph(config_factory().SQLALCHEMY_DATABASE_URI)
    pool = connection_pool_factory(
        conninfo=connection_string,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        min_size=1,
        max_size=10,
        check=ConnectionPool.check_connection,
        open=True,
    )
    saver = saver_factory(pool)
    saver.setup()
    return saver, pool


def close_connection_pool(pool: Any) -> None:
    """关闭 Saver 使用的连接池。"""
    if pool is not None:
        pool.close()


@dataclass
class CheckpointProvider:
    """管理一个进程级 Checkpoint 资源集合。"""

    config_factory: Callable[[], Any] = Config
    connection_pool_factory: Callable[..., Any] = ConnectionPool
    saver_factory: Callable[..., Any] = PostgresSaver
    register_exit: Callable[[Callable[[], None]], Any] = atexit.register
    _saver: Optional[Any] = field(default=None, init=False, repr=False)
    _pool: Optional[Any] = field(default=None, init=False, repr=False)
    _memory_saver: Optional[InMemorySaver] = field(default=None, init=False, repr=False)
    _close_registered: bool = field(default=False, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def get_postgres_saver(self) -> Any:
        """按需创建并缓存 PostgresSaver。"""
        with self._lock:
            if self._saver is None:
                self._saver, self._pool = create_postgres_saver(
                    config_factory=self.config_factory,
                    connection_pool_factory=self.connection_pool_factory,
                    saver_factory=self.saver_factory,
                )
                if not self._close_registered:
                    self.register_exit(self.close_postgres_saver)
                    self._close_registered = True
            return self._saver

    def get_configured_checkpointer(
        self,
        mode: str | None = None,
    ) -> BaseCheckpointSaver | bool:
        """解析受支持的运行时 Saver 模式。"""
        selected = (
            (mode or self.config_factory().AGENT_CHECKPOINT_BACKEND).strip().lower()
        )
        if selected == "postgres":
            return self.get_postgres_saver()
        if selected == "memory":
            with self._lock:
                if self._memory_saver is None:
                    self._memory_saver = InMemorySaver()
                return self._memory_saver
        if selected in {"disabled", "false", "off", "none"}:
            return False
        raise ValueError("AGENT_CHECKPOINT_BACKEND 必须是 postgres、memory 或 disabled")

    def close_postgres_saver(self) -> None:
        """释放 PostgresSaver 连接池并清空缓存。"""
        with self._lock:
            pool = self._pool
            self._saver = None
            self._pool = None
            close_connection_pool(pool)


_provider = CheckpointProvider()


def get_postgres_saver() -> Any:
    """使用默认配置获取进程级 PostgresSaver。"""
    return _provider.get_postgres_saver()


def get_configured_checkpointer(
    mode: str | None = None,
) -> BaseCheckpointSaver | bool:
    """使用默认配置解析运行时 Saver。"""
    return _provider.get_configured_checkpointer(mode)


def close_postgres_saver() -> None:
    """关闭默认 Provider 的 PostgresSaver。"""
    _provider.close_postgres_saver()


def build_thread_id(
    *,
    account_id: str,
    app_id: str,
    conversation_id: str,
    agent_id: str = "main",
) -> str:
    """构造稳定的 LangGraph thread_id。"""
    return f"({account_id},{app_id},{conversation_id},{agent_id})"


__all__ = [
    "BaseCheckpointSaver",
    "CheckpointProvider",
    "InMemorySaver",
    "PostgresSaver",
    "_pg_uri_for_langgraph",
    "build_thread_id",
    "close_connection_pool",
    "close_postgres_saver",
    "create_postgres_saver",
    "get_configured_checkpointer",
    "get_postgres_saver",
]
