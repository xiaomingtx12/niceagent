"""基于 LangGraph Store 的长期记忆文件管理。"""

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
from psycopg import InterfaceError as PsycopgInterfaceError
from psycopg import OperationalError as PsycopgOperationalError
from psycopg_pool import PoolTimeout

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
_STORE_CONNECTION_ERRORS = (
    PsycopgOperationalError,
    PsycopgInterfaceError,
    PoolTimeout,
)


def normalize_memory_path(path: str) -> str:
    """校验并规范 StoreBackend 使用的记忆文件路径。"""
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
    """把 Store 时间字段转换为 API 使用的 ISO 字符串。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _content_bytes(content: str, encoding: str) -> bytes:
    """按声明的编码把记忆文件内容转换为字节。"""
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
    """投影到 API 契约中的长期记忆文件。"""

    namespace: tuple[str, ...]
    path: str
    content: str
    encoding: str
    created_at: str | None
    updated_at: str | None

    @property
    def content_bytes(self) -> bytes:
        """返回文件内容的原始字节。"""
        return _content_bytes(self.content, self.encoding)

    @property
    def mime_type(self) -> str:
        """根据文件名和编码推断 MIME 类型。"""
        return mimetypes.guess_type(PurePosixPath(self.path).name)[0] or (
            "text/plain" if self.encoding == "utf-8" else "application/octet-stream"
        )

    @property
    def content_sha256(self) -> str:
        """返回文件内容的 SHA-256 摘要。"""
        return hashlib.sha256(self.content_bytes).hexdigest()

    @property
    def file_id(self) -> str:
        """根据租户命名空间和路径生成稳定文件 ID。"""
        digest = hashlib.sha256(
            f"{'|'.join(self.namespace)}|{self.path}".encode("utf-8")
        )
        return digest.hexdigest()

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        """转换为前端和管理接口使用的文件字典。"""
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
    """管理当前用户隔离的长期记忆文件，并适配 LangGraph Store。"""

    store_provider: Callable[[], BaseStore] = get_configured_store

    def _with_store_retry(
        self,
        operation: Callable[[BaseStore], Any],
    ) -> Any:
        """连接被 PostgreSQL 断开时重建 Store 并重试一次。"""
        try:
            return operation(self.store_provider())
        except _STORE_CONNECTION_ERRORS:
            if self.store_provider is get_configured_store:
                try:
                    close_postgres_store()
                except Exception:
                    pass
            try:
                return operation(self.store_provider())
            except _STORE_CONNECTION_ERRORS as second_error:
                raise FailException(
                    "长期记忆存储暂时不可用，请稍后重试"
                ) from second_error

    def list_files(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID | None = None,
    ) -> list[MemoryFile]:
        """分页读取当前作用域下的长期记忆文件。"""
        # 1.根据租户、应用和终端用户确定 Store 命名空间。
        namespace = self._namespace(account_id, app_id, end_user_id)

        def list_from_store(store: BaseStore) -> list[MemoryFile]:
            """分页查询并转换 Store 中的记忆文件条目。"""
            # 2.分页读取 Store，并过滤不属于记忆目录的条目。
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

        # 3.统一处理 Store 连接断开后的重建和重试。
        return self._with_store_retry(list_from_store)

    def read_file(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID | None,
        path: str,
    ) -> MemoryFile:
        """读取当前作用域下的单个长期记忆文件。"""
        # 1.校验虚拟路径并解析当前用户的记忆命名空间。
        key = normalize_memory_path(path)
        namespace = self._namespace(account_id, app_id, end_user_id)

        def read_from_store(store: BaseStore) -> MemoryFile:
            """从 Store 读取并转换一个记忆文件条目。"""
            item = store.get(namespace, key)
            if item is None:
                raise FailException("记忆文件不存在")
            return self._from_item(namespace, item)

        # 2.从 LangGraph Store 读取单个记忆文件。
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
        """校验并写入当前作用域的长期记忆文件。"""
        # 1.校验内容、路径和租户命名空间。
        if not isinstance(content, str):
            raise FailException("记忆文件内容必须是字符串")
        key = normalize_memory_path(path)
        namespace = self._namespace(account_id, app_id, end_user_id)

        def write_to_store(store: BaseStore) -> MemoryFile:
            """写入记忆文件并读取保存后的标准化条目。"""
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

        # 2.写入 Store 后重新读取，返回规范化后的文件对象。
        return self._with_store_retry(write_to_store)

    def delete_file(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID | None,
        path: str,
    ) -> None:
        """删除当前作用域下的长期记忆文件。"""
        # 1.校验路径和命名空间，避免跨用户删除。
        key = normalize_memory_path(path)
        namespace = self._namespace(account_id, app_id, end_user_id)

        def delete_from_store(store: BaseStore) -> None:
            """删除 Store 中指定命名空间下的记忆文件。"""
            if store.get(namespace, key) is None:
                raise FailException("记忆文件不存在")
            store.delete(namespace, key)

        # 2.删除目标文件，并复用连接异常重试机制。
        self._with_store_retry(delete_from_store)

    @staticmethod
    def _namespace(
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID | None,
    ) -> tuple[str, ...]:
        """把业务身份转换为长期记忆 Store 的租户命名空间。"""
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
        """把 LangGraph Store 条目转换为 API 层 MemoryFile。"""
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
