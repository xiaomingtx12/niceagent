"""Agent Workspace 虚拟路径的领域规则。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


WORKSPACE_VIRTUAL_ROOT = "/workspace"
MAX_WORKSPACE_VIRTUAL_PATH_LENGTH = 512
MAX_WORKSPACE_STORAGE_PATH_LENGTH = 512


class WorkspacePathError(ValueError):
    """虚拟路径违反 Workspace 命名空间或存储约束。"""


@dataclass(frozen=True, slots=True)
class AgentWorkspacePathPolicy:
    """在稳定 Workspace 作用域内转换并校验虚拟路径。"""

    scope_id: UUID

    def resolve_file(self, path: str) -> str:
        """把文件虚拟路径转换为存储路径，并拒绝 Workspace 根目录。"""
        relative = self._relative_path(path, allow_root=False)
        return self._storage_path(relative)

    def resolve_prefix(self, path: str) -> str:
        """把目录虚拟路径转换为查询前缀，并允许 Workspace 根目录。"""
        relative = self._relative_path(path, allow_root=True)
        return self._storage_path(relative)

    def virtual_path(self, storage_path: str) -> str:
        """把当前作用域内的存储路径转换为 Agent 可见路径。"""
        if len(storage_path) > MAX_WORKSPACE_STORAGE_PATH_LENGTH:
            raise WorkspacePathError("workspace path exceeds the storage limit")
        storage_root = str(self.scope_id)
        if storage_path == storage_root:
            return WORKSPACE_VIRTUAL_ROOT
        prefix = f"{storage_root}/"
        if not storage_path.startswith(prefix):
            raise WorkspacePathError("workspace path escaped its storage scope")
        relative = storage_path[len(prefix) :]
        self._validate_segments(relative, original=storage_path)
        return f"{WORKSPACE_VIRTUAL_ROOT}/{relative}"

    def contains(self, storage_path: str) -> bool:
        """判断存储路径是否是当前 Workspace 作用域内的合法路径。"""
        try:
            self.virtual_path(storage_path)
        except WorkspacePathError:
            return False
        return True

    def _relative_path(self, path: str, *, allow_root: bool) -> str:
        """提取并校验不含 Workspace 根前缀的相对路径。"""
        normalized = self._normalize(path)
        if normalized == WORKSPACE_VIRTUAL_ROOT:
            if allow_root:
                return ""
            raise WorkspacePathError("workspace root is a directory")
        prefix = f"{WORKSPACE_VIRTUAL_ROOT}/"
        if not normalized.startswith(prefix):
            raise WorkspacePathError(
                f"path '{normalized}' is not in the {prefix} namespace"
            )
        relative = normalized[len(prefix) :]
        self._validate_segments(relative, original=normalized)
        return relative

    def _storage_path(self, relative: str) -> str:
        """追加可信作用域，并保证结果可以完整写入持久化字段。"""
        storage_root = str(self.scope_id)
        storage_path = f"{storage_root}/{relative}" if relative else storage_root
        if len(storage_path) > MAX_WORKSPACE_STORAGE_PATH_LENGTH:
            raise WorkspacePathError("workspace path exceeds the storage limit")
        return storage_path

    @staticmethod
    def _normalize(path: str) -> str:
        """规范并校验 Deep Agents 使用的 POSIX 虚拟路径。"""
        if not path:
            return "/"
        if (
            "\x00" in path
            or "\\" in path
            or len(path) > MAX_WORKSPACE_VIRTUAL_PATH_LENGTH
            or any(ord(character) < 32 for character in path)
        ):
            raise WorkspacePathError("workspace path contains invalid characters")
        normalized = path.rstrip("/")
        return normalized if normalized else "/"

    @staticmethod
    def _validate_segments(relative: str, *, original: str) -> None:
        """拒绝空片段和可能改变目录语义的特殊片段。"""
        segments = relative.split("/") if relative else ()
        if (
            not segments
            or "\x00" in relative
            or "\\" in relative
            or any(ord(character) < 32 for character in relative)
            or any(
                not segment or segment in {".", "..", "~"} for segment in segments
            )
        ):
            raise WorkspacePathError(f"path '{original}' contains illegal segments")


__all__ = [
    "AgentWorkspacePathPolicy",
    "WorkspacePathError",
]
