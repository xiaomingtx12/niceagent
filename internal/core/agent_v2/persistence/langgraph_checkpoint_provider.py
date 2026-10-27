"""LangGraph checkpoint 的真实持久化实现。

这个模块只负责 Saver 和连接池。
"""

from __future__ import annotations

import atexit
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Optional

from config.config import Config
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool

from internal.core.agent_v2.persistence.postgres_connection import (
    build_langgraph_pool_config,
    execute_with_postgres_retry,
    pg_uri_for_langgraph,
)
from internal.extension.logging_extension import logger


class ResilientPostgresSaver(PostgresSaver):
    """为幂等 Checkpoint 数据库操作补充一次瞬时断连重试。"""

    def _execute(self, operation_name: str, operation: Callable[[], Any]) -> Any:
        """通过连接池新连接重试单个 Checkpoint 操作。"""
        return execute_with_postgres_retry(
            operation,
            logger=logger,
            resource="Agent checkpoint",
            operation_name=operation_name,
        )

    def setup(self) -> None:
        """幂等初始化 LangGraph 托管表。"""
        parent = super()
        self._execute("setup", parent.setup)

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """读取线程 Checkpoint，瞬时断连时重试一次。"""
        parent = super()
        return self._execute("get_tuple", lambda: parent.get_tuple(config))

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """完整读取 Checkpoint 列表后返回稳定迭代器。"""
        parent = super()
        items = self._execute(
            "list",
            lambda: list(
                parent.list(
                    config,
                    filter=filter,
                    before=before,
                    limit=limit,
                )
            ),
        )
        return iter(items)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """保存一个具备稳定 ID 的 Checkpoint。"""
        parent = super()
        return self._execute(
            "put",
            lambda: parent.put(config, checkpoint, metadata, new_versions),
        )

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """保存具备唯一约束的中间写入。"""
        parent = super()
        self._execute(
            "put_writes",
            lambda: parent.put_writes(config, writes, task_id, task_path),
        )

    def get_delta_channel_history(
        self,
        *,
        config: RunnableConfig,
        channels: Sequence[str],
    ) -> Mapping[str, Any]:
        """读取增量 channel 历史。"""
        parent = super()
        return self._execute(
            "get_delta_channel_history",
            lambda: parent.get_delta_channel_history(
                config=config,
                channels=channels,
            ),
        )

    def delete_thread(self, thread_id: str) -> None:
        """幂等删除线程的全部 Checkpoint 数据。"""
        parent = super()
        self._execute("delete_thread", lambda: parent.delete_thread(thread_id))


def create_postgres_saver(
    *,
    config_factory: Callable[[], Any] = Config,
    connection_pool_factory: Callable[..., Any] = ConnectionPool,
    saver_factory: Callable[..., Any] = ResilientPostgresSaver,
) -> tuple[Any, Any]:
    """创建并初始化一个长期存活的 PostgresSaver 和连接池。"""
    config = config_factory()
    connection_string = pg_uri_for_langgraph(config.SQLALCHEMY_DATABASE_URI)
    pool_config = build_langgraph_pool_config(
        config,
        min_size=config.AGENT_CHECKPOINT_POOL_MIN_SIZE,
        max_size=config.AGENT_CHECKPOINT_POOL_MAX_SIZE,
        timeout=config.AGENT_CHECKPOINT_POOL_TIMEOUT_SECONDS,
        max_waiting=config.AGENT_CHECKPOINT_POOL_MAX_WAITING,
        name="agent-checkpoint",
    )
    pool = connection_pool_factory(
        conninfo=connection_string,
        **pool_config,
        open=True,
    )
    try:
        # 工厂拒绝该 pool 时同样要归还连接池与 worker 线程。
        saver = saver_factory(pool)
        saver.setup()
    except Exception:
        close_connection_pool(pool)
        raise
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
    saver_factory: Callable[..., Any] = ResilientPostgresSaver
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

    def get_pool_stats(self) -> dict[str, int]:
        """返回当前进程 checkpoint pool 指标；尚未初始化时返回空字典。"""
        with self._lock:
            pool = self._pool
            if pool is None:
                return {}
            get_stats = getattr(pool, "get_stats", None)
            if not callable(get_stats):
                return {}
            return dict(get_stats())


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


def delete_agent_checkpoint(conversation_id: str) -> None:
    """删除会话的全部 checkpoint；禁用持久化时无需清理。"""
    from internal.core.agent_v2.backends.conversation_history import delete_conversation_history
    from internal.core.agent_v2.persistence.langgraph_store_provider import get_configured_store
    delete_conversation_history(get_configured_store(), conversation_id)
    checkpointer = get_configured_checkpointer()
    if checkpointer is False:
        return
    checkpointer.delete_thread(conversation_id)


def get_postgres_pool_stats() -> dict[str, int]:
    """读取默认 Provider 的连接池指标。"""
    return _provider.get_pool_stats()


__all__ = [
    "BaseCheckpointSaver",
    "CheckpointProvider",
    "InMemorySaver",
    "PostgresSaver",
    "ResilientPostgresSaver",
    "close_connection_pool",
    "close_postgres_saver",
    "create_postgres_saver",
    "delete_agent_checkpoint",
    "get_configured_checkpointer",
    "get_postgres_pool_stats",
    "get_postgres_saver",
]
