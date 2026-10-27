"""统一管理 Agent PostgreSQL 连接参数和瞬时断连重试。"""

from __future__ import annotations

from collections.abc import Callable
from logging import Logger
from typing import Any, TypeVar

from psycopg import InterfaceError as PsycopgInterfaceError
from psycopg import OperationalError as PsycopgOperationalError
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout, TooManyRequests


POSTGRES_CONNECTION_ERRORS = (
    PsycopgOperationalError,
    PsycopgInterfaceError,
)
AGENT_PERSISTENCE_ERRORS = (*POSTGRES_CONNECTION_ERRORS, PoolTimeout)
T = TypeVar("T")


def pg_uri_for_langgraph(sqlalchemy_uri: str) -> str:
    """把 SQLAlchemy driver URI 转成 psycopg 可直接连接的 URI。"""
    if sqlalchemy_uri.startswith("postgresql+"):
        return "postgresql://" + sqlalchemy_uri.split("://", 1)[1]
    return sqlalchemy_uri


def build_langgraph_pool_config(
    config: Any,
    *,
    min_size: int,
    max_size: int,
    timeout: float,
    max_waiting: int,
    name: str,
) -> dict[str, Any]:
    """构造 Checkpoint 和 Memory Store 共用的连接池策略。"""
    return {
        "kwargs": {
            "autocommit": True,
            "application_name": f"my-dify-{name}",
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "connect_timeout": config.POSTGRES_CONNECT_TIMEOUT_SECONDS,
            "keepalives": 1,
            "keepalives_idle": config.POSTGRES_KEEPALIVES_IDLE_SECONDS,
            "keepalives_interval": config.POSTGRES_KEEPALIVES_INTERVAL_SECONDS,
            "keepalives_count": config.POSTGRES_KEEPALIVES_COUNT,
        },
        "min_size": min_size,
        "max_size": max_size,
        "timeout": timeout,
        "max_waiting": max_waiting,
        "max_lifetime": config.POSTGRES_POOL_MAX_LIFETIME_SECONDS,
        "max_idle": config.POSTGRES_POOL_MAX_IDLE_SECONDS,
        "reconnect_timeout": config.POSTGRES_POOL_RECONNECT_TIMEOUT_SECONDS,
        "check": ConnectionPool.check_connection,
        "name": name,
    }


def execute_with_postgres_retry(
    operation: Callable[[], T],
    *,
    logger: Logger,
    resource: str,
    operation_name: str,
) -> T:
    """连接瞬时失效时只重试当前幂等数据库操作一次。"""
    try:
        return operation()
    except (PoolClosed, PoolTimeout, TooManyRequests):
        # 池已关闭或容量耗尽时不会因再次借连接而恢复，直接交给调用方处理。
        raise
    except POSTGRES_CONNECTION_ERRORS as exc:
        logger.warning(
            "%s PostgreSQL 连接失效，使用连接池新连接重试 operation=%s error_type=%s",
            resource,
            operation_name,
            type(exc).__name__,
        )
        return operation()


__all__ = [
    "POSTGRES_CONNECTION_ERRORS",
    "AGENT_PERSISTENCE_ERRORS",
    "build_langgraph_pool_config",
    "execute_with_postgres_retry",
    "pg_uri_for_langgraph",
]
