"""复用原生 StoreBackend，补充文件配额、原子修改与只读记忆目录。"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from deepagents.backends import StoreBackend
from deepagents.backends.protocol import (
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    WriteResult,
)
from deepagents.backends.utils import file_data_to_string, perform_string_replacement
from langgraph.store.base import BaseStore

from internal.core.agent_v2.entity.agent_memory import (
    AGENT_MEMORY_INDEX,
    AGENT_MEMORY_CATEGORIES,
    AgentMemoryIdentity,
    AgentMemoryLimits,
    AgentMemoryPathError,
    AgentMemoryQuotaExceeded,
    DEFAULT_AGENT_MEMORY_LIMITS,
    memory_store_key_to_virtual_path,
    memory_virtual_path_to_store_key,
    validate_agent_memory_content,
    validate_agent_memory_scope,
)
from internal.core.agent_v2.persistence.agent_memory_repository import (
    AgentMemoryRepository,
)
from internal.core.agent_v2.persistence.memory_file_transaction import (
    memory_file_transaction,
)


class AgentMemoryBackend(StoreBackend):
    """文件格式、读取、查找和精确替换由框架负责，业务限制集中于写入边界。"""

    def __init__(
        self,
        *,
        store: BaseStore,
        namespace: Callable[[Any], tuple[str, ...]],
        limits: AgentMemoryLimits = DEFAULT_AGENT_MEMORY_LIMITS,
    ) -> None:
        """命名空间由可信运行上下文解析，不接受模型提供账号。"""
        super().__init__(store=store, namespace=namespace, file_format="v2")
        self._limits = limits

    def write(self, file_path: str, content: str) -> WriteResult:
        """在同一作用域锁中校验配额并调用原生新建，禁止覆盖已有文件。"""
        try:
            key = self._write_key(file_path)
            namespace = self._get_namespace()
            with memory_file_transaction(self._get_store(), namespace) as store:
                self._validate_content(store, namespace, key, content)
                return StoreBackend(
                    store=store, namespace=lambda _: namespace, file_format="v2"
                ).write(key, content)
        except (ValueError, TypeError) as exc:
            return WriteResult(error=self._error_message(exc))

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """读、替换、容量检查和提交持有同一把锁，避免不同会话互相覆盖。"""
        try:
            key = self._write_key(file_path)
            namespace = self._get_namespace()
            with memory_file_transaction(self._get_store(), namespace) as store:
                native = StoreBackend(
                    store=store, namespace=lambda _: namespace, file_format="v2"
                )
                item = store.get(namespace, key)
                if item is None:
                    return EditResult(error="记忆文件不存在，请先读取最新目录")
                replacement = perform_string_replacement(
                    file_data_to_string(self._convert_store_item_to_file_data(item)),
                    old_string,
                    new_string,
                    replace_all,
                )
                if isinstance(replacement, str):
                    return EditResult(error=replacement)
                self._validate_content(store, namespace, key, replacement[0])
                return native.edit(key, old_string, new_string, replace_all)
        except (ValueError, TypeError) as exc:
            return EditResult(error=self._error_message(exc))

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """目录即时生成，不作为用户文件保存，也不会被编辑或误删。"""
        responses = []
        for path in paths:
            if path == "/.index.md":
                responses.append(
                    FileDownloadResponse(
                        path=path, content=self._index().encode("utf-8")
                    )
                )
            else:
                responses.extend(super().download_files([path]))
        return responses

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """平台上传同样遵循配额和原子边界，不留旁路覆盖能力。"""
        responses = []
        for path, content in files:
            try:
                key = self._write_key(path)
                text = content.decode("utf-8")
                namespace = self._get_namespace()
                with memory_file_transaction(self._get_store(), namespace) as store:
                    self._validate_content(store, namespace, key, text)
                    responses.extend(
                        StoreBackend(
                            store=store, namespace=lambda _: namespace, file_format="v2"
                        ).upload_files([(key, content)])
                    )
            except (ValueError, TypeError) as exc:
                responses.append(
                    FileUploadResponse(path=path, error=self._error_message(exc))
                )
        return responses

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """平台提供同步 PostgresStore，在线程中复用同一事务实现。"""
        return await asyncio.to_thread(self.write, file_path, content)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """异步调用保持与同步工具相同的并发约束。"""
        return await asyncio.to_thread(
            self.edit, file_path, old_string, new_string, replace_all
        )

    async def aread(self, *args, **kwargs):
        """避免对同步 PostgresStore 调用未实现的异步方法。"""
        return await asyncio.to_thread(self.read, *args, **kwargs)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """原生 MemoryMiddleware 异步加载使用同一动态目录。"""
        return await asyncio.to_thread(self.download_files, paths)

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        """异步平台上传复用同步校验和提交。"""
        return await asyncio.to_thread(self.upload_files, files)

    def _index(self) -> str:
        """限制自动注入长度；常用偏好整篇加载，其他文件只列路径供按需读取。"""
        namespace = self._get_namespace()
        identity = AgentMemoryIdentity(account_id=namespace[-2], app_id=namespace[-1])
        rows = AgentMemoryRepository(store_provider=self._get_store).list_files(
            identity
        )
        rows.sort(key=lambda row: not row.path.startswith("/memories/preferences/"))
        body = "记忆目录（正文未列出的文件需要时使用 read_file 读取）：\n"
        for row in rows:
            if row.path == AGENT_MEMORY_INDEX:
                continue
            folder = row.path.removeprefix("/memories/").split("/")[0]
            line = (
                f"\n- {row.path}（{AGENT_MEMORY_CATEGORIES.get(folder, '背景信息')}）"
            )
            if len((body + line).encode("utf-8")) > 3500:
                body += "\n其余文件使用 ls /memories/ 查看。"
                break
            body += line
        for row in rows:
            if row.path.startswith("/memories/preferences/") and row.size_bytes <= 900:
                block = f"\n\n偏好全文 {row.path}:\n{row.content}"
                if len((body + block).encode("utf-8")) <= 3600:
                    body += block
        return body

    def _validate_content(self, store, namespace, key: str, content: str) -> None:
        """配额校验基于锁内最新内容，避免并发写入绕过上限。"""
        size = validate_agent_memory_content(content, limits=self._limits)
        items = store.search(namespace, limit=self._limits.max_files + 1)
        sizes = {
            item.key: len(item.value.get("content", "").encode("utf-8"))
            for item in items
        }
        validate_agent_memory_scope(
            file_count=len(sizes) + int(key not in sizes),
            total_bytes=sum(sizes.values()) - sizes.get(key, 0) + size,
            limits=self._limits,
        )

    @staticmethod
    def _write_key(path: str) -> str:
        """只读内部目录不能被通用文件工具修改。"""
        virtual = memory_store_key_to_virtual_path(path)
        if virtual == AGENT_MEMORY_INDEX:
            raise AgentMemoryPathError("记忆索引由系统生成，请编辑分类下的记忆文件")
        return memory_virtual_path_to_store_key(virtual)

    @staticmethod
    def _error_message(exc: Exception) -> str:
        """返回可供模型修正参数的领域错误，不伪造成功回执。"""
        return (
            exc.user_message if isinstance(exc, AgentMemoryQuotaExceeded) else str(exc)
        )
