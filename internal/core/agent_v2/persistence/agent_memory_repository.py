"""Agent 长期记忆的 LangGraph Store 访问实现。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Callable, TypeVar

from langgraph.store.base import BaseStore
from psycopg import InterfaceError as PsycopgInterfaceError
from psycopg import OperationalError as PsycopgOperationalError
from psycopg_pool import PoolTimeout

from internal.core.agent_v2.entity.agent_memory import (
    AGENT_MEMORY_NAMESPACE_ROOT,
    AgentMemoryFile,
    AgentMemoryIdentity,
    AgentMemoryConflict,
    validate_agent_memory_content,
    validate_agent_memory_scope,
    memory_store_key_to_virtual_path,
    memory_virtual_path_to_store_key,
)
from .memory_file_transaction import memory_file_transaction
from internal.core.agent_v2.persistence.langgraph_store_provider import (
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
    """统一转换虚拟路径、Store key 并映射 Store 连接错误。"""

    store_provider: Callable[[], BaseStore] = get_configured_store

    def list_files(self, identity: AgentMemoryIdentity) -> list[AgentMemoryFile]:
        """列出当前主体命名空间中的全部文本记忆。"""

        def operation(store: BaseStore) -> list[AgentMemoryFile]:
            self._import_entries(store, identity)
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
        original_path: str | None = None,
        expected_sha256: str | None = None,
    ) -> AgentMemoryFile:
        """新增或覆盖一个 UTF-8 文本记忆。"""
        key = memory_virtual_path_to_store_key(path)
        old_key = memory_virtual_path_to_store_key(original_path or path)

        def operation(base_store: BaseStore) -> AgentMemoryFile:
            with memory_file_transaction(base_store, identity.namespace) as store:
                existing = store.get(identity.namespace, old_key)
                self._check_expected(existing, expected_sha256)
                if old_key != key and store.get(identity.namespace, key) is not None:
                    raise AgentMemoryConflict("该分类中已有同名记忆，请换一个名称")
                if old_key != key and existing is None:
                    raise AgentMemoryConflict("原记忆已被删除，请刷新列表")
                size = validate_agent_memory_content(content)
                rows = self._search_all(store, identity.namespace)
                validate_agent_memory_scope(
                    file_count=len(rows) + int(existing is None),
                    total_bytes=sum(
                        len(item.value.get("content", "").encode("utf-8"))
                        for item in rows
                        if item.key != old_key
                    )
                    + size,
                )
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
                if old_key != key:
                    store.delete(identity.namespace, old_key)
                saved = store.get(identity.namespace, key)
                if saved is None:
                    raise AgentMemoryStorageError("记忆文件保存后无法读取")
                return self._item_to_entity(saved)

        return self._execute(operation)

    def delete_file(
        self,
        identity: AgentMemoryIdentity,
        path: str,
        *,
        expected_sha256: str | None = None,
    ) -> bool:
        """删除一个记忆文件，返回目标是否存在。"""
        key = memory_virtual_path_to_store_key(path)

        def operation(base_store: BaseStore) -> bool:
            with memory_file_transaction(base_store, identity.namespace) as store:
                item = store.get(identity.namespace, key)
                if item is None:
                    return False
                self._check_expected(item, expected_sha256)
                store.delete(identity.namespace, key)
                return True

        return self._execute(operation)

    @classmethod
    def _check_expected(cls, item: Any, expected: str | None) -> None:
        """空摘要表示只允许新建，非空摘要防止自动保存覆盖已变更文件。"""
        if expected is None:
            return
        actual = cls._item_to_entity(item).content_sha256 if item else ""
        if actual != expected:
            raise AgentMemoryConflict("记忆已被修改或删除，请重新打开后再编辑")

    @classmethod
    def _import_entries(
        cls, base_store: BaseStore, identity: AgentMemoryIdentity
    ) -> None:
        """仅首次导入有效条目；完成标识独立于文件，删除后不会再次导入。"""
        metadata = (
            "mydify",
            "agent-memory-files-meta",
            str(identity.account_id),
            str(identity.app_id),
        )
        if base_store.get(metadata, "imported") is not None:
            return
        with memory_file_transaction(base_store, identity.namespace) as store:
            if store.get(metadata, "imported") is not None:
                return
            namespace = (
                "mydify",
                "agent-memory-entries",
                str(identity.account_id),
                str(identity.app_id),
            )
            catalog = store.get(namespace, "catalog")
            entries = catalog.value.get("entries", []) if catalog else []
            planned = {}
            occupied = {item.key for item in cls._search_all(store, identity.namespace)}
            for entry in entries:
                if entry.get("status") != "active":
                    continue
                category = {
                    "preference": "preferences",
                    "fact": "background",
                    "experience": "experience",
                }[entry["category"]]
                title = (
                    re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", entry["topic"])[:70] or "记忆"
                )
                key = f"/{category}/{title}-{entry['id'][:8]}.md"
                suffix = 2
                while key in occupied:
                    key = f"/{category}/{title}-{entry['id'][:8]}-{suffix}.md"
                    suffix += 1
                occupied.add(key)
                row = AgentMemoryFile(
                    path=memory_store_key_to_virtual_path(key),
                    content=(
                        f"适用范围：{entry['scope']}\n\n{entry['content']}"
                        if entry.get("scope") and entry["scope"] != "当前应用"
                        else entry["content"]
                    ),
                    created_at=entry["created_at"],
                    updated_at=entry["updated_at"],
                )
                planned[key] = {
                    "content": row.content,
                    "encoding": "utf-8",
                    "created_at": entry["created_at"],
                    "modified_at": entry["updated_at"],
                }
            # 先验证全部导入内容；旧数据允许超出新建配额，不能因此截断已有记忆。
            for key, value in planned.items():
                store.put(identity.namespace, key, value)
            # 旧 MEMORY.md 可能含已暂停或已纠正内容，沿用原有字面抑制规则。
            if entries:
                from internal.core.agent_v2.entity.memory_entry import (
                    normalized_memory_text,
                )

                suppressed = {
                    normalized_memory_text(value)
                    for entry in entries
                    for value in [
                        entry.get("topic", ""),
                        entry.get("content", ""),
                        *(
                            revision.get("content", "")
                            for revision in entry.get("revisions", [])
                        ),
                    ]
                    if len(normalized_memory_text(value)) >= 2
                }
                legacy = store.get(identity.namespace, "/MEMORY.md")
                if legacy and isinstance(legacy.value.get("content"), str):
                    content = "\n".join(
                        line
                        for line in legacy.value["content"].splitlines()
                        if not any(
                            term in normalized_memory_text(line) for term in suppressed
                        )
                    )
                    if content.strip():
                        store.put(
                            identity.namespace,
                            "/MEMORY.md",
                            {**legacy.value, "content": content},
                        )
                    else:
                        store.delete(identity.namespace, "/MEMORY.md")
                store.delete(namespace, "catalog")
            store.put(metadata, "imported", {"done": True})

    def delete_app_memories(self, *, account_id: str, app_id: str) -> None:
        """删除账号下应用的全部长期记忆。"""
        prefix = (*AGENT_MEMORY_NAMESPACE_ROOT, account_id, app_id)

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

        # 结构化目录使用独立 namespace，同一应用清理必须覆盖两种存储。
        from internal.core.agent_v2.persistence.memory_entry_repository import (
            MemoryEntryRepository,
        )

        identity = AgentMemoryIdentity(account_id=account_id, app_id=app_id)
        self._execute(
            lambda store: store.delete(
                MemoryEntryRepository.namespace(identity), "catalog"
            )
        )
        self._execute(
            lambda store: store.delete(
                ("mydify", "agent-memory-files-meta", str(account_id), str(app_id)),
                "imported",
            )
        )

    def _execute(self, operation: Callable[[BaseStore], T]) -> T:
        """把 Store 连接错误转换为稳定的领域异常。"""
        try:
            return operation(self.store_provider())
        except _STORE_CONNECTION_ERRORS as exc:
            # 默认 PostgreSQL Store 已在单次 batch 内完成新连接重试；这里不关闭
            # 进程共享连接池，避免一个请求的断连级联打断其他并发请求。
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
