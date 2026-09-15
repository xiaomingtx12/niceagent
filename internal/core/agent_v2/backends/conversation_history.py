"""框架归档独立于 Workspace：可信图作用域、有限容量和原子追加。"""

import asyncio
import hashlib
import re

from deepagents.backends import StoreBackend
from deepagents.backends.protocol import EditResult, FileUploadResponse, WriteResult
from deepagents.backends.utils import create_file_data, file_data_to_string, update_file_data
from langgraph.runtime import get_runtime

from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.persistence.memory_file_transaction import memory_file_transaction

HISTORY_PREFIX = ("mydify", "agent-conversation-history")
MAX_HISTORY_FILE_BYTES = 8 * 1024 * 1024
MAX_HISTORY_SCOPE_BYTES = 32 * 1024 * 1024
MAX_HISTORY_FILES = 128


def history_namespace(runtime) -> tuple[str, ...]:
    """保留完整父图作用域，去掉当前节点后主图读写一致，同名子图仍隔离。"""
    context = runtime.context
    if not isinstance(context, AgentRunContext):
        raise RuntimeError("归档访问需要可信 AgentRunContext")
    execution = runtime.execution_info
    if execution is None:
        raise RuntimeError("归档访问需要有效图执行作用域")
    # LangGraph 1.x 的 ExecutionInfo 包含当前 node:task_id；它每步变化，不是图身份。
    scope = execution.checkpoint_ns.rpartition("|")[0]
    return history_scope(context.conversation_id, context.account_id, context.app_id, scope)


def history_scope(conversation_id, account_id, app_id, scope="") -> tuple[str, ...]:
    """会话置于前缀便于连同全部子图清理；所有参数由服务端授权结果提供。"""
    return (*HISTORY_PREFIX, str(conversation_id), str(account_id), str(app_id),
            hashlib.sha256(scope.encode()).hexdigest() if scope else "root")


class ConversationHistoryBackend(StoreBackend):
    """只供框架内部写入；模型写权限由 FilesystemPermission 显式拒绝。"""

    def write(self, file_path, content):
        """归档重试允许相同内容；冲突不覆盖已有原文。"""
        return self._save(file_path, content, previous=None)

    def edit(self, file_path, old_string, new_string, replace_all=False):
        """只接受框架的完整追加，拒绝局部改写和并发覆盖。"""
        result = self._save(file_path, new_string, previous=old_string)
        return EditResult(error=result.error, path=result.path, occurrences=0 if result.error else 1)

    def _save(self, path, content, *, previous):
        """作用域锁内去重并校验总容量，超过上限让原生压缩器标记归档失败。"""
        namespace = self._get_namespace()
        with memory_file_transaction(self._get_store(), namespace) as store:
            existing = store.get(namespace, path)
            data = self._convert_store_item_to_file_data(existing) if existing else None
            current = file_data_to_string(data) if data else ""
            appended = content[len(previous or ""):]
            append_id = _append_identity(appended) if path.endswith(".md") else None
            if current == content or (append_id and existing and existing.value.get("archive_append_id") == append_id):
                return WriteResult(path=path)
            if previous is None and existing:
                return WriteResult(error="归档文件已存在且内容不同")
            if previous is not None and (previous != current or not content.startswith(current)):
                return WriteResult(error="归档追加冲突，请保留原历史")
            error = self._quota_error(store, namespace, path, len(content.encode("utf-8")))
            if error:
                return WriteResult(error=error)
            value = update_file_data(data, content) if data else create_file_data(content)
            stored = self._convert_file_data_to_store_value(value)
            if append_id:
                stored["archive_append_id"] = append_id
            store.put(namespace, path, stored)
        return WriteResult(path=path)

    def _quota_error(self, store, namespace, path, size):
        """原文与媒体共用独立限额，不挤占长期记忆配额。"""
        if size > MAX_HISTORY_FILE_BYTES:
            return "历史归档单文件超过 8 MiB 上限"
        items = store.search(namespace, limit=MAX_HISTORY_FILES + 1)
        others = [item for item in items if item.key != path]
        if len(others) >= MAX_HISTORY_FILES:
            return "历史归档超过 128 个文件上限"
        if size + sum(len(str(item.value.get("content", "")).encode("utf-8")) for item in others) > MAX_HISTORY_SCOPE_BYTES:
            return "历史归档作用域超过 32 MiB 上限"
        return None

    def upload_files(self, files):
        """媒体由框架上传，使用相同事务限额；失败不能伪装成已保存引用。"""
        results = []
        namespace = self._get_namespace()
        for path, content in files:
            with memory_file_transaction(self._get_store(), namespace) as store:
                # base64 存储按编码后字节预留，避免二进制绕过总量限制。
                encoded_size = 4 * ((len(content) + 2) // 3)
                if encoded_size > MAX_HISTORY_FILE_BYTES:
                    results.append(FileUploadResponse(path=path, error="历史媒体超过单文件 8 MiB 上限"))
                    continue
                error = self._quota_error(store, namespace, path, encoded_size)
                if error:
                    results.append(FileUploadResponse(path=path, error=error))
                    continue
                backend = StoreBackend(store=store, namespace=lambda _: namespace, file_format="v2")
                results.extend(backend.upload_files([(path, content)]))
        return results

    async def awrite(self, file_path, content):
        """同步 PostgreSQL Store 的事务在工作线程中执行。"""
        return await asyncio.to_thread(self.write, file_path, content)

    async def aedit(self, file_path, old_string, new_string, replace_all=False):
        """异步追加沿用同一事务与去重边界。"""
        return await asyncio.to_thread(self.edit, file_path, old_string, new_string, replace_all)

    async def aread(self, file_path, offset=0, limit=2000):
        """异步文件读取不依赖同步 Store 提供异步方法。"""
        return await asyncio.to_thread(self.read, file_path, offset, limit)

    async def adownload_files(self, paths):
        """原生异步压缩读取已有原文时复用同步存储。"""
        return await asyncio.to_thread(self.download_files, paths)

    async def aupload_files(self, files):
        """异步媒体写入同样接受容量控制。"""
        return await asyncio.to_thread(self.upload_files, files)


def _append_identity(section):
    """相同 checkpoint 的重试去重；不同轮次即使文字相同也分别归档。"""
    if not section.startswith("## Summarized at "):
        return None
    execution = get_runtime().execution_info
    if execution is None or not execution.checkpoint_id:
        return None
    body = re.sub(r"^## Summarized at [^\n]+\n", "", section, count=1)
    return hashlib.sha256(f"{execution.checkpoint_id}:{body}".encode()).hexdigest()


def delete_conversation_history(store, conversation_id):
    """随会话或消息清理删除归档与媒体，包括全部子图；不触碰长期记忆。"""
    if store is None:
        return
    prefix = (*HISTORY_PREFIX, str(conversation_id))
    while items := store.search(prefix, limit=100):
        for item in items:
            store.delete(item.namespace, item.key)
