"""长期记忆文件管理用例。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from injector import inject

from internal.core.agent_v2.entity.agent_memory import (
    AgentMemoryFile,
    AgentMemoryIdentity,
    AgentMemoryPathError,
    AgentMemoryQuotaExceeded,
    DEFAULT_AGENT_MEMORY_LIMITS,
    normalize_agent_memory_path,
    validate_agent_memory_content,
    validate_agent_memory_scope,
)
from internal.core.agent_v2.persistence.agent_memory_repository import (
    AgentMemoryRepository,
    AgentMemoryStorageError,
)
from internal.exception import FailException, ValidateErrorException


@inject
@dataclass
class MemoryFileService:
    """协调记忆身份、领域校验、配额和持久化。"""

    agent_memory_repository: AgentMemoryRepository

    def list_files(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
    ) -> list[AgentMemoryFile]:
        """列出当前账号、应用的长期记忆。"""
        identity = self._identity(account_id, app_id)
        try:
            return self.agent_memory_repository.list_files(identity)
        except AgentMemoryStorageError as exc:
            raise FailException(str(exc)) from exc

    def read_file(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        path: str,
    ) -> AgentMemoryFile:
        """读取当前账号、应用的一个记忆文件。"""
        identity = self._identity(account_id, app_id)
        try:
            normalized_path = normalize_agent_memory_path(path)
            row = self.agent_memory_repository.get_file(identity, normalized_path)
        except AgentMemoryPathError as exc:
            raise ValidateErrorException(str(exc)) from exc
        except AgentMemoryStorageError as exc:
            raise FailException(str(exc)) from exc
        if row is None:
            raise FailException("记忆文件不存在")
        return row

    def write_file(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        path: str,
        content: str,
    ) -> AgentMemoryFile:
        """按配额新增或覆盖当前账号、应用的 UTF-8 文本记忆。"""
        identity = self._identity(account_id, app_id)
        try:
            normalized_path = normalize_agent_memory_path(path)
            size = validate_agent_memory_content(content)
            rows = self.agent_memory_repository.list_files(identity)
            existing = next(
                (row for row in rows if row.path == normalized_path),
                None,
            )
            validate_agent_memory_scope(
                file_count=len(rows) + (0 if existing is not None else 1),
                total_bytes=(
                    sum(row.size_bytes for row in rows)
                    - (existing.size_bytes if existing is not None else 0)
                    + size
                ),
                limits=DEFAULT_AGENT_MEMORY_LIMITS,
            )
            return self.agent_memory_repository.put_file(
                identity,
                path=normalized_path,
                content=content,
            )
        except (AgentMemoryPathError, AgentMemoryQuotaExceeded, TypeError) as exc:
            message = (
                exc.user_message
                if isinstance(exc, AgentMemoryQuotaExceeded)
                else str(exc)
            )
            raise ValidateErrorException(message) from exc
        except AgentMemoryStorageError as exc:
            raise FailException(str(exc)) from exc

    def delete_file(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        path: str,
    ) -> None:
        """删除当前账号、应用的一个长期记忆文件。"""
        identity = self._identity(account_id, app_id)
        try:
            normalized_path = normalize_agent_memory_path(path)
            deleted = self.agent_memory_repository.delete_file(
                identity,
                normalized_path,
            )
        except AgentMemoryPathError as exc:
            raise ValidateErrorException(str(exc)) from exc
        except AgentMemoryStorageError as exc:
            raise FailException(str(exc)) from exc
        if not deleted:
            raise FailException("记忆文件不存在")

    @staticmethod
    def _identity(
        account_id: str | UUID,
        app_id: str | UUID,
    ) -> AgentMemoryIdentity:
        """把服务边界 ID 转换为账号级长期记忆身份。"""
        try:
            return AgentMemoryIdentity(
                account_id=UUID(str(account_id)),
                app_id=UUID(str(app_id)),
            )
        except (TypeError, ValueError) as exc:
            raise ValidateErrorException("记忆文件作用域不是合法 UUID") from exc


__all__ = ["MemoryFileService"]
