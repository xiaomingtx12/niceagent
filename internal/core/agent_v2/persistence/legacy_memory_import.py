"""旧结构化记忆到文件记忆的兼容转换；仅由首次列表读取触发。"""

from __future__ import annotations

import re
from typing import Any

from langgraph.store.base import BaseStore

from internal.core.agent_v2.entity.agent_memory import (
    AgentMemoryFile,
    AgentMemoryIdentity,
    memory_store_key_to_virtual_path,
)
from internal.core.agent_v2.entity.memory_entry import normalized_memory_text
from .memory_file_transaction import memory_file_transaction


def legacy_entries_namespace(identity: AgentMemoryIdentity) -> tuple[str, ...]:
    """兼容导入和应用删除共用旧目录的存储位置。"""
    return ("mydify", "agent-memory-entries", str(identity.account_id), str(identity.app_id))


def legacy_import_metadata_namespace(identity: AgentMemoryIdentity) -> tuple[str, ...]:
    """导入标记独立于文件，删除文件不会再次导入旧目录。"""
    return ("mydify", "agent-memory-files-meta", str(identity.account_id), str(identity.app_id))


def ensure_legacy_memories_imported(base_store: BaseStore, identity: AgentMemoryIdentity) -> None:
    """在原有文件事务中一次性转换；失败时保留旧目录供后续读取重试。"""
    metadata = legacy_import_metadata_namespace(identity)
    if base_store.get(metadata, "imported") is not None:
        return
    with memory_file_transaction(base_store, identity.namespace) as store:
        if store.get(metadata, "imported") is not None:
            return
        namespace = legacy_entries_namespace(identity)
        catalog = store.get(namespace, "catalog")
        entries = catalog.value.get("entries", []) if catalog else []
        planned = _plan_files(store, identity, entries)
        # 全部转换和校验完成后再写入，保留已有记忆，不应用新建配额截断旧数据。
        for key, value in planned.items():
            store.put(identity.namespace, key, value)
        if entries:
            _suppress_legacy_summary(store, identity, entries)
            store.delete(namespace, "catalog")
        store.put(metadata, "imported", {"done": True})


def _plan_files(
    store: BaseStore, identity: AgentMemoryIdentity, entries: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """生成并校验全部文件，使用稳定后缀避免覆盖现有文件或同名条目。"""
    planned = {}
    occupied = _occupied_keys(store, identity.namespace)
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
    return planned


def _suppress_legacy_summary(
    store: BaseStore, identity: AgentMemoryIdentity, entries: list[dict[str, Any]],
) -> None:
    """沿用字面抑制规则，避免旧摘要复活已暂停或已纠正的记忆。"""
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


def _occupied_keys(store: BaseStore, namespace: tuple[str, ...]) -> set[str]:
    """在转换事务内读取已有文件名，覆盖超过一页的旧记忆。"""
    keys: set[str] = set()
    offset = 0
    while True:
        page = store.search(namespace, limit=100, offset=offset)
        keys.update(item.key for item in page)
        if len(page) < 100:
            return keys
        offset += 100
