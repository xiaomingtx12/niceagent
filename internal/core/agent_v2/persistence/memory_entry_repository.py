"""在现有 LangGraph Store 中原子保存结构化记忆，无新增数据库表。"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager, nullcontext
from threading import RLock
from uuid import uuid4

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres import PostgresStore
from psycopg_pool import ConnectionPool

from internal.core.agent_v2.entity.agent_memory import AgentMemoryIdentity
from internal.core.agent_v2.entity.memory_entry import (
    MAX_MEMORY_ENTRIES, MAX_MEMORY_REVISIONS, MemoryChange, MemoryConflict,
    MemoryEntry, MemoryRevision, MemorySource, memory_now, normalized_memory_text,
)

_MEMORY_LOCK = RLock()
_CATALOG_KEY = "catalog"


class MemoryEntryRepository:
    """每个账号、应用一个有界目录；版本与历史在同一 Store 写入内提交。"""

    def __init__(self, store: BaseStore) -> None:
        """复用运行时或 HTTP 用例持有的 Store，不管理它的生命周期。"""
        self.store = store

    def list_entries(self, identity: AgentMemoryIdentity) -> list[MemoryEntry]:
        """管理接口返回有界完整目录，运行时自行筛选有效条目。"""
        return self._read(self.store, self.namespace(identity))

    def change(self, identity: AgentMemoryIdentity, change: MemoryChange, source: MemorySource) -> dict:
        """串行检查版本、去重和容量，成功后一次提交目录及其历史。"""
        namespace = self.namespace(identity)
        with self._transaction(namespace) as store:
            entries = self._read(store, namespace)
            if source.operation_id:
                for entry in entries:
                    if any(r.source.operation_id == source.operation_id for r in entry.revisions):
                        return {"changed": False, "entry": entry.model_dump(mode="json")}
            entry = self._apply(entries, change, source)
            if entry is None:
                existing = self._duplicate(entries, change)
                return {"changed": False, "entry": existing.model_dump(mode="json")}
            payload = {"entries": [item.model_dump(mode="json") for item in entries]}
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 1024 * 1024:
                raise ValueError("结构化记忆及版本记录总容量不能超过 1 MiB")
            store.put(namespace, _CATALOG_KEY, payload)
            return {"changed": True, "entry": entry.model_dump(mode="json")}

    @staticmethod
    def namespace(identity: AgentMemoryIdentity) -> tuple[str, ...]:
        """独立命名空间避免结构化数据被旧文件工具或文件 API 覆盖。"""
        return ("mydify", "agent-memory-entries", str(identity.account_id), str(identity.app_id))

    @staticmethod
    def _read(store: BaseStore, namespace: tuple[str, ...]) -> list[MemoryEntry]:
        """校验持久化格式，损坏数据不能被静默当作空目录覆盖。"""
        item = store.get(namespace, _CATALOG_KEY)
        if item is None:
            return []
        return [MemoryEntry.model_validate(entry) for entry in item.value["entries"]]

    @classmethod
    def _apply(cls, entries: list[MemoryEntry], change: MemoryChange, source: MemorySource) -> MemoryEntry | None:
        """服务端确定候选状态，遗忘和暂停的条目只能由用户重新启用。"""
        now = memory_now()
        if change.action == "add":
            if not normalized_memory_text(change.topic) or not normalized_memory_text(change.content):
                raise ValueError("新增记忆必须提供主题和内容")
            duplicate = cls._duplicate(entries, change)
            if duplicate:
                if any(normalized_memory_text(r.content) == normalized_memory_text(change.content) for r in duplicate.revisions):
                    return None
                raise MemoryConflict(f"该主题已有记忆，请读取并修正 {duplicate.id}（版本 {duplicate.version}）")
            if len(entries) >= MAX_MEMORY_ENTRIES:
                raise ValueError("记忆目录最多保留 100 条，包含暂停和遗忘记录")
            entry = MemoryEntry(
                id=str(uuid4()), topic=change.topic, scope=change.scope, category=change.category,
                content=change.content, source=source, created_at=now, updated_at=now,
                status="candidate" if source.kind == "agent" else "active",
            )
            entries.append(entry)
        else:
            entry = next((item for item in entries if item.id == change.memory_id), None)
            if entry is None:
                raise ValueError("记忆不存在")
            if entry.version != change.expected_version:
                raise MemoryConflict("记忆已被更新，请刷新后基于最新版本修改")
            if source.kind == "agent" and (entry.status in {"forgotten", "paused"} or change.action in {"restore", "activate"}):
                raise ValueError("暂停或遗忘的记忆只能由用户在记忆管理中恢复")
            if change.action == "correct":
                if not normalized_memory_text(change.content):
                    raise ValueError("修正记忆必须提供内容")
                entry.content = change.content
                entry.status = "candidate" if source.kind == "agent" else "active"
            elif change.action == "restore":
                revision = next((r for r in entry.revisions if r.version == change.restore_version), None)
                if revision is None:
                    raise ValueError("目标版本不在最近 20 个版本中")
                entry.content, entry.status = revision.content, revision.status
            else:
                entry.status = {"forget": "forgotten", "pause": "paused", "activate": "active"}[change.action]
            entry.version += 1
            entry.source, entry.updated_at = source, now
        entry.revisions.append(MemoryRevision(
            version=entry.version, action=change.action, content=entry.content,
            status=entry.status, source=source, updated_at=now,
        ))
        entry.revisions = entry.revisions[-MAX_MEMORY_REVISIONS:]
        return entry

    @staticmethod
    def _duplicate(entries: list[MemoryEntry], change: MemoryChange) -> MemoryEntry | None:
        """同时检查历史内容和主题；遗忘记录仍参与去重，阻止旧会话自动复活它。"""
        for entry in entries:
            if normalized_memory_text(entry.scope) != normalized_memory_text(change.scope):
                continue
            same_topic = normalized_memory_text(entry.topic) == normalized_memory_text(change.topic)
            same_content = any(normalized_memory_text(r.content) == normalized_memory_text(change.content) for r in entry.revisions)
            if same_topic or same_content:
                return entry
        return None

    @contextmanager
    def _transaction(self, namespace: tuple[str, ...]):
        """PostgreSQL 使用事务级作用域锁；内存模式用进程锁，均覆盖完整读改写。"""
        if isinstance(self.store, InMemoryStore):
            with _MEMORY_LOCK:
                yield self.store
            return
        if not isinstance(self.store, PostgresStore):
            raise RuntimeError("结构化记忆需要 PostgreSQL 或内存 Store")
        key = int.from_bytes(hashlib.sha256("/".join(namespace).encode()).digest()[:8], "big", signed=True)
        connection = self.store.conn
        manager = connection.connection() if isinstance(connection, ConnectionPool) else nullcontext(connection)
        with manager as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
            yield PostgresStore(conn)
