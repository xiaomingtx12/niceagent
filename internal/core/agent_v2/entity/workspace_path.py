"""Agent Workspace 虚拟路径的领域规则。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from uuid import UUID

from internal.exception.agent_workspace_exception import WorkspacePathError


def build_workspace_path_digest(path: str) -> str:
    """生成不暴露真实路径的稳定遥测标识。"""
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def normalize_workspace_path(path: str) -> str:
    """规范 Deep Agents 使用的 POSIX 虚拟路径。"""
    if not path:
        return "/"
    if (
        "\x00" in path
        or "\\" in path
        or len(path) > 512
        or any(ord(character) < 32 for character in path)
    ):
        raise WorkspacePathError("workspace paths must use POSIX separators")
    normalized = path.rstrip("/")
    return normalized if normalized else "/"


def resolve_workspace_storage_path(path: str, conversation_id: UUID) -> str:
    """把 Agent 虚拟路径映射为会话隔离的存储路径。"""
    normalized = normalize_workspace_path(path)
    if normalized == "/workspace":
        return str(conversation_id)
    if not normalized.startswith("/workspace/"):
        raise WorkspacePathError(
            f"path '{normalized}' is not in the /workspace/ namespace"
        )

    relative = normalized.removeprefix("/workspace/")
    if not relative:
        raise WorkspacePathError("workspace root is a directory")
    relative_segments = relative.split("/")
    if len(relative_segments) >= 2:
        try:
            UUID(relative_segments[0])
            UUID(relative_segments[1])
        except ValueError:
            pass
        else:
            raise WorkspacePathError(
                "physical tenant-scoped workspace paths are not supported"
            )

    storage_path = f"{conversation_id}/{relative}"
    segments = storage_path.split("/")
    for segment in segments:
        if not segment or segment in (".", "..", "~"):
            raise WorkspacePathError(
                f"path '{normalized}' contains illegal segment '{segment}'"
            )
    if segments[0] != str(conversation_id):
        raise WorkspacePathError("workspace path escaped the conversation scope")
    return storage_path


def workspace_conversation_id(
    storage_path: str,
    conversation_id: UUID | None,
) -> UUID | None:
    """返回存储路径所属的会话，无法匹配时返回空。"""
    if conversation_id is None:
        return None
    first = storage_path.split("/", 1)[0]
    return conversation_id if first == str(conversation_id) else None


@dataclass(frozen=True)
class AgentWorkspacePathPolicy:
    """解析并校验当前会话内的 Agent Workspace 路径。"""

    conversation_id: UUID

    def resolve(self, path: str) -> str:
        """返回当前会话允许访问的存储路径。"""
        return resolve_workspace_storage_path(path, self.conversation_id)

    def virtual_path(self, storage_path: str) -> str:
        """把存储路径转换为 Agent 可见路径。"""
        conversation_prefix = f"{self.conversation_id}/"
        if storage_path.startswith(conversation_prefix):
            visible = storage_path[len(conversation_prefix) :]
            return f"/workspace/{visible}"
        raise WorkspacePathError("workspace path escaped the conversation scope")

    def visible_prefixes(self) -> tuple[str, ...]:
        """返回当前会话可见的存储前缀。"""
        return (str(self.conversation_id),)

    def is_visible_storage_path(self, storage_path: str) -> bool:
        """检查存储路径是否可以暴露给 Agent。"""
        return storage_path == str(self.conversation_id) or storage_path.startswith(
            f"{self.conversation_id}/"
        )


__all__ = [
    "AgentWorkspacePathPolicy",
    "build_workspace_path_digest",
    "normalize_workspace_path",
    "resolve_workspace_storage_path",
    "workspace_conversation_id",
]
