"""R0 deepagents 持久化 saver。

把 master 老 `agent_checkpoints` 表替换为 LangGraph 原生 `PostgresSaver`——
state 直接落 LangGraph 自带表 `checkpoint_*`，由 LangGraph SDK 自己托管。

约束（硬约束 #5：能用 deepagents / langchain 就用，禁止造轮子）：
- 不再写 my-dify 自己的 `agent_checkpoints` 表
- 不再写 `BaseAgent._agent_queue_manager` 的内存断点续传

thread_id 约定：
    f"({account_id}, {app_id}, {conversation_id}, 'main')"
f-string 出来的值由 LangGraph SDK 自动创建 checkpoint_blobs/checkpoint_writes
等行，跨重启可恢复。
"""

from __future__ import annotations

from threading import Lock
from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver

from config.config import Config


_saver_singleton: Optional[PostgresSaver] = None
_saver_lock = Lock()


def get_postgres_saver() -> PostgresSaver:
    """进程级 PostgresSaver 单例（线程安全）。

    LangGraph SDK 要求 saver 的 `.setup()` 在第一次 `aget`/`aput` 之前调用一次，
    用来建表。统一在这里 setup，后续业务侧只调 `get_postgres_saver()`。
    """
    global _saver_singleton
    with _saver_lock:
        if _saver_singleton is None:
            connection_string = _pg_uri_for_langgraph(Config().SQLALCHEMY_DATABASE_URI)
            _saver_singleton = PostgresSaver.from_conn_string(connection_string)
            _saver_saver_setup(_saver_singleton)
        return _saver_singleton


def _pg_uri_for_langgraph(sqlalchemy_uri: str) -> str:
    """SQLAlchemy URI 转 LangGraph 兼容 URI。

    SQLAlchemy 1.4+ 用 `postgresql+psycopg://`，LangGraph 0.6 暂不支持 `+psycopg`
    后缀，回退到 `postgresql://`（psycopg2/psycopg3 都兼容 driver-less 形式）。
    """
    if sqlalchemy_uri.startswith("postgresql+"):
        return "postgresql://" + sqlalchemy_uri.split("://", 1)[1]
    return sqlalchemy_uri


def _saver_saver_setup(saver: PostgresSaver) -> None:
    """统一调用 setup()，幂等。

    0.6 之前是 .setup()，0.6 引入了 Alembic 迁移；我们走 setup() 即可（同步建表，
    跟现有 alembic upgrade 流程独立——LangGraph 自己管表）。
    """
    # setup() 在已存在表的情况下是 no-op
    saver.setup()


def build_thread_id(
    *,
    account_id: str,
    app_id: str,
    conversation_id: str,
    agent_id: str = "main",
) -> str:
    """构造 LangGraph thread_id。

    一个 conversation_id + agent_id 唯一对应一个 deepagent state 子树：
    - 单 agent：agent_id="main"
    - 多 agent（R3）：主 agent "main"，sub-agent 用自己的 name（与
      message_agent_thoughts.parent_agent_id 对齐）

    注：LangGraph thread_id 是字符串（不是 tuple），状态隔离依靠前缀。
    """
    return f"({account_id},{app_id},{conversation_id},{agent_id})"


__all__ = [
    "BaseCheckpointSaver",
    "PostgresSaver",
    "build_thread_id",
    "get_postgres_saver",
]