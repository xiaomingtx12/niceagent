"""复用原生 StoreBackend 按需访问记忆，补充文件配额与原子修改。"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from deepagents.backends import StoreBackend
from deepagents.backends.protocol import (
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import (
    file_data_to_string,
    perform_string_replacement,
    update_file_data,
)
from langgraph.store.base import BaseStore

from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.execution.memory_updates import publish_memory_update
from internal.extension.logging_extension import logger

from internal.core.agent_v2.entity.agent_memory import (
    AgentMemoryLimits,
    AgentMemoryQuotaExceeded,
    DEFAULT_AGENT_MEMORY_LIMITS,
    memory_store_key_to_virtual_path,
    memory_virtual_path_to_store_key,
    validate_agent_memory_content,
    validate_agent_memory_scope,
)
from internal.core.agent_v2.persistence.memory_file_transaction import (
    memory_file_transaction,
)


def memory_namespace_from_runtime(runtime: Any) -> tuple[str, ...]:
    """每次存储访问从可信运行上下文获取身份，不能由模型参数或历史状态决定。"""
    context = getattr(runtime, "context", None)
    if not isinstance(context, AgentRunContext):
        raise RuntimeError("AgentRunContext is required for memory Store access")
    return context.memory_namespace


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

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """仅统计真实按需读取耗时，不记录路径、正文或追加数据库探测。"""
        return self._observe_read("read", lambda: super(AgentMemoryBackend, self).read(file_path, offset, limit))

    def ls(self, path):
        """目录读取的实际调用单独计数，便于识别不必要的轮次。"""
        return self._observe_read("ls", lambda: super(AgentMemoryBackend, self).ls(path))

    def glob(self, pattern, path=None):
        """观测路径检索，不把可能包含用户信息的模式写入日志。"""
        return self._observe_read("glob", lambda: super(AgentMemoryBackend, self).glob(pattern, path))

    def grep(self, pattern, path=None, glob=None):
        """观测内容检索；查询正文和命中内容均不进入日志。"""
        return self._observe_read("grep", lambda: super(AgentMemoryBackend, self).grep(pattern, path, glob))

    def _observe_read(self, operation, read):
        """每次操作仅记录一条诊断数据，异常仍由原调用链处理。"""
        started = time.monotonic()
        try:
            return read()
        finally:
            logger.debug("agent_memory_io operation=%s duration_ms=%.2f", operation, (time.monotonic() - started) * 1000)

    def write(self, file_path: str, content: str) -> WriteResult:
        """在同一作用域锁中校验配额并调用原生新建，禁止覆盖已有文件。"""
        try:
            key = self._write_key(file_path)
            namespace = self._get_namespace()
            with memory_file_transaction(self._get_store(), namespace) as store:
                self._validate_content(store, namespace, key, content)
                result = StoreBackend(
                    store=store, namespace=lambda _: namespace, file_format="v2"
                ).write(key, content)
                if result.error:
                    return result
        except (ValueError, TypeError) as exc:
            return WriteResult(error=self._error_message(exc))
        publish_memory_update(
            path=memory_store_key_to_virtual_path(key), action="write"
        )
        return result

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
                item = store.get(namespace, key)
                if item is None:
                    return EditResult(error="记忆文件不存在，请确认路径后再操作")
                file_data = self._convert_store_item_to_file_data(item)
                current_content = file_data_to_string(file_data)
                replacement = perform_string_replacement(
                    current_content,
                    old_string,
                    new_string,
                    replace_all,
                )
                if isinstance(replacement, str):
                    return EditResult(error=replacement)
                new_content, occurrences = replacement
                if new_content == current_content:
                    logger.debug("agent_memory_write outcome=unchanged operation=edit")
                    return EditResult(path=key, occurrences=int(occurrences))
                self._validate_content(store, namespace, key, new_content)
                updated = update_file_data(file_data, new_content)
                store.put(namespace, key, self._convert_file_data_to_store_value(updated))
        except (ValueError, TypeError) as exc:
            return EditResult(error=self._error_message(exc))
        publish_memory_update(
            path=memory_store_key_to_virtual_path(key), action="write"
        )
        return EditResult(path=key, occurrences=int(occurrences))

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """平台上传同样遵循配额和原子边界，不留旁路覆盖能力。"""
        responses = []
        for path, content in files:
            try:
                key = self._write_key(path)
                text = content.decode("utf-8")
                namespace = self._get_namespace()
                with memory_file_transaction(self._get_store(), namespace) as store:
                    existing = store.get(namespace, key)
                    if (
                        existing is not None
                        and existing.value.get("encoding", "utf-8") == "utf-8"
                        and existing.value.get("content") == text
                    ):
                        responses.append(FileUploadResponse(path=key, error=None))
                        logger.debug("agent_memory_write outcome=unchanged operation=upload")
                        continue
                    self._validate_content(store, namespace, key, text)
                    result = StoreBackend(
                        store=store, namespace=lambda _: namespace, file_format="v2"
                    ).upload_files([(key, content)])[0]
                    if result.error:
                        responses.append(result)
                        continue
            except (ValueError, TypeError) as exc:
                responses.append(
                    FileUploadResponse(path=path, error=self._error_message(exc))
                )
                continue
            responses.append(result)
            publish_memory_update(
                path=memory_store_key_to_virtual_path(key), action="write"
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

    async def aread(
        self, file_path: str, offset: int = 0, limit: int = 2000
    ) -> ReadResult:
        """避免对同步 PostgresStore 调用未实现的异步方法。"""
        return await asyncio.to_thread(self.read, file_path, offset, limit)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """下载只在调用时访问文件，不对同步 Store 使用异步 API。"""
        return await asyncio.to_thread(self.download_files, paths)

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        """异步平台上传复用同步校验和提交。"""
        return await asyncio.to_thread(self.upload_files, files)

    def _validate_content(
        self, store: BaseStore, namespace: tuple[str, ...], key: str, content: str
    ) -> None:
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
        """工具收到相对路由路径，统一按记忆作用域校验后写入。"""
        return memory_virtual_path_to_store_key(memory_store_key_to_virtual_path(path))

    @staticmethod
    def _error_message(exc: Exception) -> str:
        """返回可供模型修正参数的领域错误，不伪造成功回执。"""
        return (
            exc.user_message if isinstance(exc, AgentMemoryQuotaExceeded) else str(exc)
        )
