"""受容量和文本约束保护的 Deep Agents 长期记忆 Backend。"""

from __future__ import annotations

from typing import Any, Callable

from deepagents.backends import StoreBackend
from deepagents.backends.protocol import (
    EditResult,
    FileUploadResponse,
    WriteResult,
)
from deepagents.backends.utils import file_data_to_string, perform_string_replacement
from langgraph.store.base import BaseStore

from internal.core.agent_v2.entity.agent_memory import (
    AgentMemoryLimits,
    AgentMemoryPathError,
    AgentMemoryQuotaExceeded,
    DEFAULT_AGENT_MEMORY_LIMITS,
    memory_store_key_to_virtual_path,
    memory_virtual_path_to_store_key,
    validate_agent_memory_content,
    validate_agent_memory_scope,
)


class AgentMemoryBackend(StoreBackend):
    """复用官方 StoreBackend，并限制记忆为小体量 UTF-8 文本。

    本类只增加路径、编码和容量校验；实际 Store 读写仍交给
    DeepAgents 官方实现，避免复制持久化格式和查询语义。
    """

    def __init__(
        self,
        *,
        store: BaseStore,
        namespace: Callable[[Any], tuple[str, ...]],
        limits: AgentMemoryLimits = DEFAULT_AGENT_MEMORY_LIMITS,
    ) -> None:
        """保存 Store、动态命名空间和业务容量上限。"""
        super().__init__(store=store, namespace=namespace, file_format="v2")
        self._limits = limits

    def write(self, file_path: str, content: str) -> WriteResult:
        """在官方写入语义前校验路径、文本大小和作用域容量。"""
        try:
            key = self._normalize_store_key(file_path)
            store = self._get_store()
            namespace = self._get_namespace()
            self._validate_projected_content(
                store=store,
                namespace=namespace,
                key=key,
                content=content,
            )
        except (AgentMemoryPathError, AgentMemoryQuotaExceeded, TypeError) as exc:
            return WriteResult(error=self._error_message(exc))
        return super().write(key, content)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """异步校验后使用 Store 原生异步接口写入。"""
        try:
            key = self._normalize_store_key(file_path)
            store = self._get_store()
            namespace = self._get_namespace()
            await self._avalidate_projected_content(
                store=store,
                namespace=namespace,
                key=key,
                content=content,
            )
        except (AgentMemoryPathError, AgentMemoryQuotaExceeded, TypeError) as exc:
            return WriteResult(error=self._error_message(exc))
        return await super().awrite(key, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """先计算编辑后的完整文本，再执行官方精确替换。"""
        try:
            key = self._normalize_store_key(file_path)
            store = self._get_store()
            namespace = self._get_namespace()
            item = store.get(namespace, key)
            if item is None:
                return super().edit(key, old_string, new_string, replace_all)
            file_data = self._convert_store_item_to_file_data(item)
            if file_data.get("encoding") != "utf-8":
                return EditResult(error="记忆文件只支持 UTF-8 文本")
            replacement = perform_string_replacement(
                file_data_to_string(file_data),
                old_string,
                new_string,
                replace_all,
            )
            if isinstance(replacement, str):
                return EditResult(error=replacement)
            self._validate_projected_content(
                store=store,
                namespace=namespace,
                key=key,
                content=replacement[0],
            )
        except (AgentMemoryPathError, AgentMemoryQuotaExceeded, TypeError) as exc:
            return EditResult(error=self._error_message(exc))
        return super().edit(key, old_string, new_string, replace_all)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """异步计算最终文本并校验后执行官方编辑。"""
        try:
            key = self._normalize_store_key(file_path)
            store = self._get_store()
            namespace = self._get_namespace()
            item = await store.aget(namespace, key)
            if item is None:
                return await super().aedit(key, old_string, new_string, replace_all)
            file_data = self._convert_store_item_to_file_data(item)
            if file_data.get("encoding") != "utf-8":
                return EditResult(error="记忆文件只支持 UTF-8 文本")
            replacement = perform_string_replacement(
                file_data_to_string(file_data),
                old_string,
                new_string,
                replace_all,
            )
            if isinstance(replacement, str):
                return EditResult(error=replacement)
            await self._avalidate_projected_content(
                store=store,
                namespace=namespace,
                key=key,
                content=replacement[0],
            )
        except (AgentMemoryPathError, AgentMemoryQuotaExceeded, TypeError) as exc:
            return EditResult(error=self._error_message(exc))
        return await super().aedit(key, old_string, new_string, replace_all)

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """先完整校验批次，再一次性写入合法的 UTF-8 文本。"""
        prepared: list[tuple[int, str, bytes]] = []
        responses: list[FileUploadResponse | None] = [None] * len(files)
        store = self._get_store()
        namespace = self._get_namespace()
        sizes = self._stored_sizes(
            store.search(namespace, limit=self._limits.max_files + 1)
        )
        for index, (path, raw_content) in enumerate(files):
            try:
                content = raw_content.decode("utf-8")
                key = self._normalize_store_key(path)
                size = validate_agent_memory_content(content, limits=self._limits)
                projected_count = len(sizes) + (0 if key in sizes else 1)
                projected_total = sum(sizes.values()) - sizes.get(key, 0) + size
                validate_agent_memory_scope(
                    file_count=projected_count,
                    total_bytes=projected_total,
                    limits=self._limits,
                )
            except UnicodeDecodeError:
                responses[index] = FileUploadResponse(
                    path=path,
                    error="记忆文件只支持 UTF-8 文本",
                )
                continue
            except (
                AgentMemoryPathError,
                AgentMemoryQuotaExceeded,
                TypeError,
            ) as exc:
                responses[index] = FileUploadResponse(
                    path=path,
                    error=self._error_message(exc),
                )
                continue
            sizes[key] = size
            prepared.append((index, key, raw_content))

        if prepared:
            upload_responses = super().upload_files(
                [(key, raw_content) for _, key, raw_content in prepared]
            )
            for (index, _, _), response in zip(
                prepared,
                upload_responses,
                strict=True,
            ):
                responses[index] = response
        return [response for response in responses if response is not None]

    def _validate_projected_content(
        self,
        *,
        store: BaseStore,
        namespace: tuple[str, ...],
        key: str,
        content: str,
    ) -> None:
        """按目标写入后的状态校验文件数与总字节数。"""
        size = validate_agent_memory_content(content, limits=self._limits)
        sizes = self._stored_sizes(
            store.search(namespace, limit=self._limits.max_files + 1)
        )
        validate_agent_memory_scope(
            file_count=len(sizes) + (0 if key in sizes else 1),
            total_bytes=sum(sizes.values()) - sizes.get(key, 0) + size,
            limits=self._limits,
        )

    async def _avalidate_projected_content(
        self,
        *,
        store: BaseStore,
        namespace: tuple[str, ...],
        key: str,
        content: str,
    ) -> None:
        """使用 Store 异步查询校验目标写入后的容量。"""
        size = validate_agent_memory_content(content, limits=self._limits)
        items = await store.asearch(namespace, limit=self._limits.max_files + 1)
        sizes = self._stored_sizes(items)
        validate_agent_memory_scope(
            file_count=len(sizes) + (0 if key in sizes else 1),
            total_bytes=sum(sizes.values()) - sizes.get(key, 0) + size,
            limits=self._limits,
        )

    @staticmethod
    def _stored_sizes(items: list[Any]) -> dict[str, int]:
        """统计 Store 中 UTF-8 文本条目的字节数。"""
        sizes: dict[str, int] = {}
        for item in items:
            value = item.value
            content = value.get("content") if isinstance(value, dict) else None
            sizes[str(item.key)] = (
                len(content.encode("utf-8")) if isinstance(content, str) else 0
            )
        return sizes

    @staticmethod
    def _normalize_store_key(path: str) -> str:
        """校验 CompositeBackend 去掉前缀后的相对 key。"""
        virtual_path = memory_store_key_to_virtual_path(path)
        return memory_virtual_path_to_store_key(virtual_path)

    @staticmethod
    def _error_message(exc: Exception) -> str:
        """把领域异常转换为文件工具可理解的稳定错误。"""
        if isinstance(exc, AgentMemoryQuotaExceeded):
            return exc.user_message
        return str(exc)


__all__ = ["AgentMemoryBackend"]
