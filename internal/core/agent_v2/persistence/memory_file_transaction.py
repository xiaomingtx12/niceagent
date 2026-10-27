"""文件工具和管理接口共享同一记忆作用域的原子读改写。"""

import hashlib
from contextlib import contextmanager, nullcontext
from threading import RLock

from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres import PostgresStore
from psycopg_pool import ConnectionPool

_LOCK = RLock()


@contextmanager
def memory_file_transaction(store, namespace):
    """数据库锁跨进程生效；内存 Store 使用进程锁供本地运行与测试。"""
    if isinstance(store, InMemoryStore):
        with _LOCK:
            yield store
        return
    if not isinstance(store, PostgresStore):
        raise RuntimeError("文件记忆需要 PostgreSQL 或内存 Store")
    key = int.from_bytes(
        hashlib.sha256("/".join(namespace).encode()).digest()[:8], "big", signed=True
    )
    connection = store.conn
    manager = (
        connection.connection()
        if isinstance(connection, ConnectionPool)
        else nullcontext(connection)
    )
    with manager as conn, conn.transaction():
        conn.execute("SELECT set_config('lock_timeout', '5000', true)")
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        yield PostgresStore(conn)
