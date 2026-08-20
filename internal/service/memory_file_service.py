"""Long-term memory file management backed by LangGraph Store."""

from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Callable
from uuid import UUID

from langgraph.store.base import BaseStore
from psycopg import OperationalError as PsycopgOperationalError

from internal.core.agent_v2.backends.workspace_path import (
    contains_reserved_segment,
)
from internal.core.agent_v2.persistence.store import (
    close_postgres_store,
    get_configured_store,
)
from internal.core.agent_v2.runtime.models import build_memory_namespace
from internal.exception import FailException

MEMORY_ROOT = "/memories/"
MAX_MEMORY_PATH_LENGTH = 512
_STORE_PAGE_SIZE = 100


def normalize_memory_path(path: str) -> str:
    """Validate and normalize a StoreBackend memory file key."""
    normalized = (path or "").strip().replace("\\", "/")
    if not normalized.startswith(MEMORY_ROOT):
        raise FailException("记忆文件路径必须以 /memories/ 开头")

    relative = normalized[len(MEMORY_ROOT) :].strip("/")
    segments = relative.split("/") if relative else []
    if (
        not segments
        or len(normalized) > MAX_MEMORY_PATH_LENGTH
        or "\x00" in normalized
        or any(
            part in {"", ".", "..", "~"}
            or any(ord(character) < 32 for character in part)
            for part in segments
        )
        or contains_reserved_segment(relative)
    ):
        raise FailException("记忆文件路径不合法")
    return f"{MEMORY_ROOT}{'/'.join(segments)}"


def _isoformat(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _content_bytes(content: str, encoding: str) -> bytes:
    if encoding == "utf-8":
        return content.encode("utf-8")
    if encoding == "base64":
        try:
            return base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise FailException("记忆文件内容编码损坏") from exc
    raise FailException("记忆文件编码不受支持")


@dataclass(frozen=True)
class MemoryFile:
    """A StoreBackend file projected into the API contract."""

    namespace: tuple[str, ...]
    path: str
    content: str
    encoding: str
    created_at: str | None
    updated_at: str | None

    @property
    def content_bytes(self) -> bytes:
        return _content_bytes(self.content, self.encoding)

    @property
    def mime_type(self) -> str:
        return mimetypes.guess_type(PurePosixPath(self.path).name)[0] or (
            "text/plain" if self.encoding == "utf-8" else "application/octet-stream"
        )

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content_bytes).hexdigest()

    @property
    def file_id(self) -> str:
        digest = hashlib.sha256(
            f"{'|'.join(self.namespace)}|{self.path}".encode("utf-8")
        )
        return digest.hexdigest()

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.file_id,
            "path": self.path,
            "virtual_path": self.path,
            "scope": "memory",
            "persistent": True,
            "storage_backend": "langgraph_store",
            "encoding": self.encoding,
            "mime_type": self.mime_type,
            "size_bytes": len(self.content_bytes),
            "content_sha256": self.content_sha256,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_content:
            data["content"] = self.content if self.encoding == "utf-8" else None
        return data


@dataclass
class MemoryFileService:
    """List and mutate the current user's persistent memory files."""

    store_provider: Callable[[], BaseStore] = get_configured_store

    def _with_store_retry(
        self,
        operation: Callable[[BaseStore], Any],
    ) -> Any:
        """连接被 PostgreSQL 断开时重建 Store 并重试一次。"""
        store = self.store_provider()
        try:
            return operation(store)
        except PsycopgOperationalError:
            if self.store_provider is get_configured_store:
                close_postgres_store()
            return operation(self.store_provider())

    def list_files(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID | None = None,
    ) -> list[MemoryFile]:
        namespace = self._namespace(account_id, app_id, end_user_id)

        def list_from_store(store: BaseStore) -> list[MemoryFile]:
            rows: list[MemoryFile] = []
            offset = 0
            while True:
                items = store.search(
                    namespace,
                    limit=_STORE_PAGE_SIZE,
                    offset=offset,
                )
                if not items:
                    break
                for item in items:
                    path = str(item.key)
                    if not path.startswith(MEMORY_ROOT):
                        continue
                    try:
                        rows.append(self._from_item(namespace, item))
                    except FailException:
                        continue
                if len(items) < _STORE_PAGE_SIZE:
                    break
                offset += _STORE_PAGE_SIZE
            return sorted(
                rows,
                key=lambda item: item.updated_at or "",
                reverse=True,
            )

        return self._with_store_retry(list_from_store)

    def read_file(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID | None,
        path: str,
    ) -> MemoryFile:
        key = normalize_memory_path(path)
        namespace = self._namespace(account_id, app_id, end_user_id)

        def read_from_store(store: BaseStore) -> MemoryFile:
            item = store.get(namespace, key)
            if item is None:
                raise FailException("记忆文件不存在")
            return self._from_item(namespace, item)

        return self._with_store_retry(read_from_store)

    def write_file(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID | None,
        path: str,
        content: str,
    ) -> MemoryFile:
        if not isinstance(content, str):
            raise FailException("记忆文件内容必须是字符串")
        key = normalize_memory_path(path)
        namespace = self._namespace(account_id, app_id, end_user_id)

        def write_to_store(store: BaseStore) -> MemoryFile:
            existing = store.get(namespace, key)
            old_value = existing.value if existing is not None else {}
            created_at = (
                old_value.get("created_at")
                if isinstance(old_value, dict)
                else None
            )
            now = datetime.now(timezone.utc).isoformat()
            store.put(
                namespace,
                key,
                {
                    "content": content,
                    "encoding": "utf-8",
                    "created_at": created_at or now,
                    "modified_at": now,
                },
            )
            item = store.get(namespace, key)
            if item is None:
                raise FailException("记忆文件保存失败")
            return self._from_item(namespace, item)

        return self._with_store_retry(write_to_store)

    def delete_file(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID | None,
        path: str,
    ) -> None:
        key = normalize_memory_path(path)
        namespace = self._namespace(account_id, app_id, end_user_id)

        def delete_from_store(store: BaseStore) -> None:
            if store.get(namespace, key) is None:
                raise FailException("记忆文件不存在")
            store.delete(namespace, key)

        self._with_store_retry(delete_from_store)

    @staticmethod
    def _namespace(
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID | None,
    ) -> tuple[str, ...]:
        try:
            account_uuid = UUID(str(account_id))
            app_uuid = UUID(str(app_id))
            end_user_uuid = UUID(str(end_user_id)) if end_user_id else None
        except (TypeError, ValueError) as exc:
            raise FailException("记忆文件作用域不是合法 UUID") from exc
        return build_memory_namespace(
            account_id=account_uuid,
            app_id=app_uuid,
            end_user_id=end_user_uuid,
        )

    @staticmethod
    def _from_item(namespace: tuple[str, ...], item: Any) -> MemoryFile:
        path = normalize_memory_path(str(item.key))
        value = item.value
        if not isinstance(value, dict) or not isinstance(value.get("content"), str):
            raise FailException("记忆文件内容格式不受支持")
        encoding = value.get("encoding", "utf-8")
        if not isinstance(encoding, str):
            raise FailException("记忆文件编码不受支持")
        return MemoryFile(
            namespace=namespace,
            path=path,
            content=value["content"],
            encoding=encoding,
            created_at=_isoformat(
                value.get("created_at", getattr(item, "created_at", None))
            ),
            updated_at=_isoformat(
                value.get("modified_at", getattr(item, "updated_at", None))
            ),
        )


__all__ = [
    "MEMORY_ROOT",
    "MemoryFile",
    "MemoryFileService",
    "normalize_memory_path",
]
