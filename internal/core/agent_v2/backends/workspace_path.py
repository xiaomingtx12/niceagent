"""Workspace 虚拟路径规范与作用域策略。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Optional
from uuid import UUID

_RESERVED_SEGMENTS = frozenset({".audit", ".system"})


class WorkspacePathError(ValueError):
    """虚拟路径越过允许作用域时抛出。"""


def contains_reserved_segment(path: str) -> bool:
    """检查路径是否进入私有命名空间。"""
    return any(segment in _RESERVED_SEGMENTS for segment in path.split("/"))


def workspace_path_digest(path: str) -> str:
    """生成不暴露路径的稳定遥测标识。"""
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def normalize_workspace_path(path: str) -> str:
    """规范 DeepAgents POSIX 路径。"""
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


def validate_workspace_path(
    path: str,
    conversation_id: UUID,
    account_id: UUID | None = None,
    app_id: UUID | None = None,
) -> str:
    """把唯一的 Agent 虚拟路径映射为会话存储路径。"""
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
    rest = f"{conversation_id}/{relative}"
    if not rest:
        raise WorkspacePathError(f"path '{normalized}' has no storage scope")
    segments = rest.split("/")
    for segment in segments:
        if not segment or segment in (".", "..", "~"):
            raise WorkspacePathError(
                f"path '{normalized}' contains illegal segment '{segment}'"
            )
    if contains_reserved_segment(rest):
        raise WorkspacePathError(
            f"path '{normalized}' enters a reserved namespace"
        )

    if segments[0] != str(conversation_id):
        raise WorkspacePathError("workspace path escaped the conversation scope")
    return rest


def scope_to_conversation_id(
    rest: str, conversation_id: UUID
) -> Optional[UUID]:
    """把存储路径作用域映射到所属会话。"""
    first = rest.split("/", 1)[0]
    if first == str(conversation_id):
        return conversation_id
    return None


@dataclass(frozen=True)
class WorkspacePathPolicy:
    """解析当前会话内的 Agent 路径。"""

    conversation_id: UUID
    account_id: UUID | None = None
    app_id: UUID | None = None

    def resolve(self, path: str) -> str:
        """返回当前作用域允许的存储路径。"""
        return validate_workspace_path(
            path,
            self.conversation_id,
            account_id=self.account_id,
            app_id=self.app_id,
        )

    def virtual_path(self, rest: str) -> str:
        """把存储路径转换为 Agent 可见形式。"""
        conversation_prefix = f"{self.conversation_id}/"
        if rest.startswith(conversation_prefix):
            visible = rest[len(conversation_prefix):]
            return f"/workspace/{visible}"
        raise WorkspacePathError("workspace path escaped the conversation scope")

    def visible_prefixes(self) -> tuple[str]:
        """返回当前会话存储前缀。"""
        return (str(self.conversation_id),)

    def is_visible_storage_path(self, rest: str) -> bool:
        """检查存储路径是否可安全暴露。"""
        return not contains_reserved_segment(rest)
