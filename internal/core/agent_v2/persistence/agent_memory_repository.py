"""Agent 长期记忆的 LangGraph Store 访问实现。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from langgraph.store.base import BaseStore
from psycopg import InterfaceError as PsycopgInterfaceError
from psycopg import OperationalError as PsycopgOperationalError
from psycopg_pool import PoolTimeout

from internal.core.agent_v2.entity.agent_memory import (
    AGENT_MEMORY_NAMESPACE_ROOT,
    AgentMemoryFile,
    AgentMemoryIdentity,
    memory_store_key_to_virtual_path,
    memory_virtual_path_to_store_key,
)
from internal.core.agent_v2.persistence.langgraph_store_provider import (
    close_postgres_store,
    get_configured_store,
)


_STORE_PAGE_SIZE = 100
_STORE_CONNECTION_ERRORS = (
    PsycopgOperationalError,
    PsycopgInterfaceError,
    PoolTimeout,
)
T = TypeVar("T")


class AgentMemoryStorageError(RuntimeError):
    """长期记忆 Store 不可用或返回无效数据。"""


@dataclass
class AgentMemoryRepository:
    """统一转换虚拟路径、Store key 并管理 Store 连接重试。"""

    store_provider: Callable[[], BaseStore] = get_configured_store

    def list_files(self, identity: AgentMemoryIdentity) -> list[AgentMemoryFile]:
        """列出当前主体命名空间中的全部文本记忆。"""

        def operation(store: BaseStore) -> list[AgentMemoryFile]:
            rows = [
                self._item_to_entity(item)
                for item in self._search_all(store, identity.namespace)
            ]
            return sorted(
                rows,
                key=lambda row: str(row.updated_at or ""),
                reverse=True,
            )

        return self._execute(operation)

    def get_file(
        self,
        identity: AgentMemoryIdentity,
        path: str,
    ) -> AgentMemoryFile | None:
        """读取一个虚拟路径对应的 Store 条目。"""
        key = memory_virtual_path_to_store_key(path)

        def operation(store: BaseStore) -> AgentMemoryFile | None:
            item = store.get(identity.namespace, key)
            return self._item_to_entity(item) if item is not None else None

        return self._execute(operation)

    def put_file(
        self,
        identity: AgentMemoryIdentity,
        *,
        path: str,
        content: str,
    ) -> AgentMemoryFile:
        """新增或覆盖一个 UTF-8 文本记忆。"""
        key = memory_virtual_path_to_store_key(path)

        def operation(store: BaseStore) -> AgentMemoryFile:
            existing = store.get(identity.namespace, key)
            created_at = None
            if existing is not None and isinstance(existing.value, dict):
                created_at = existing.value.get("created_at")
            now = datetime.now(timezone.utc).isoformat()
            store.put(
                identity.namespace,
                key,
                {
                    "content": content,
                    "encoding": "utf-8",
                    "created_at": created_at or now,
                    "modified_at": now,
                },
            )
            saved = store.get(identity.namespace, key)
            if saved is None:
                raise AgentMemoryStorageError("记忆文件保存后无法读取")
            return self._item_to_entity(saved)

        return self._execute(operation)

    def delete_file(self, identity: AgentMemoryIdentity, path: str) -> bool:
        """删除一个记忆文件，返回目标是否存在。"""
        key = memory_virtual_path_to_store_key(path)

        def operation(store: BaseStore) -> bool:
            if store.get(identity.namespace, key) is None:
                return False
            store.delete(identity.namespace, key)
            return True

        return self._execute(operation)

    def delete_app_memories(self, *, tenant_id: str, app_id: str) -> None:
        """删除应用下所有主体的长期记忆。"""
        prefix = (*AGENT_MEMORY_NAMESPACE_ROOT, tenant_id, app_id)

        def operation(store: BaseStore) -> None:
            namespaces: list[tuple[str, ...]] = []
            offset = 0
            while True:
                page = store.list_namespaces(
                    prefix=prefix,
                    limit=_STORE_PAGE_SIZE,
                    offset=offset,
                )
                if not page:
                    break
                namespaces.extend(page)
                if len(page) < _STORE_PAGE_SIZE:
                    break
                offset += _STORE_PAGE_SIZE
            for namespace in namespaces:
                for item in self._search_all(store, namespace):
                    store.delete(namespace, item.key)

        self._execute(operation)

    def _execute(self, operation: Callable[[BaseStore], T]) -> T:
        """连接失效时重建默认 Store 并重试一次。"""
        try:
            return operation(self.store_provider())
        except _STORE_CONNECTION_ERRORS:
            if self.store_provider is get_configured_store:
                close_postgres_store()
            try:
                return operation(self.store_provider())
            except _STORE_CONNECTION_ERRORS as exc:
                raise AgentMemoryStorageError("长期记忆存储暂时不可用") from exc

    @staticmethod
    def _search_all(
        store: BaseStore,
        namespace: tuple[str, ...],
    ) -> list[Any]:
        """分页读取一个完整命名空间。"""
        items: list[Any] = []
        offset = 0
        while True:
            page = store.search(
                namespace,
                limit=_STORE_PAGE_SIZE,
                offset=offset,
            )
            if not page:
                break
            items.extend(page)
            if len(page) < _STORE_PAGE_SIZE:
                break
            offset += _STORE_PAGE_SIZE
        return items

    @staticmethod
    def _item_to_entity(item: Any) -> AgentMemoryFile:
        """把 Store v2 文件条目转换为领域实体。"""
        value = item.value
        if (
            not isinstance(value, dict)
            or value.get("encoding", "utf-8") != "utf-8"
            or not isinstance(value.get("content"), str)
        ):
            raise AgentMemoryStorageError("记忆 Store 中存在非 UTF-8 文本条目")
        return AgentMemoryFile(
            path=memory_store_key_to_virtual_path(str(item.key)),
            content=value["content"],
            created_at=AgentMemoryRepository._parse_datetime(
                value.get("created_at", getattr(item, "created_at", None))
            ),
            updated_at=AgentMemoryRepository._parse_datetime(
                value.get("modified_at", getattr(item, "updated_at", None))
            ),
        )

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        """把 Store 时间戳规范为响应 Schema 可序列化的 datetime。"""
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise AgentMemoryStorageError("记忆 Store 时间字段格式错误") from exc
        if value is None:
            return None
        raise AgentMemoryStorageError("记忆 Store 时间字段格式错误")


__all__ = ["AgentMemoryRepository", "AgentMemoryStorageError"]
