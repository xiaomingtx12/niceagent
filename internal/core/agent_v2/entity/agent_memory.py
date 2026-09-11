"""Agent 长期记忆的身份、路径和容量规则。"""

from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import PurePosixPath
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


AGENT_MEMORY_NAMESPACE_ROOT = ("mydify", "agent-memory")
AGENT_MEMORY_VIRTUAL_ROOT = "/memories/"
AGENT_MEMORY_INDEX = "/memories/.index.md"
AGENT_MEMORY_CATEGORIES = {
    "preferences": "用户偏好",
    "background": "背景信息",
    "experience": "经验方法",
}
MAX_AGENT_MEMORY_PATH_LENGTH = 512


class AgentMemoryPathError(ValueError):
    """记忆路径越过 `/memories/` 作用域或包含非法片段。"""


class AgentMemoryQuotaExceeded(ValueError):
    """记忆文件数量、单文件大小或总容量超过限制。"""

    def __init__(self, limit: str, maximum: int) -> None:
        """记录超限维度，不携带记忆内容。"""
        self.limit = limit
        self.maximum = maximum
        super().__init__(f"Agent memory {limit} quota exceeded ({maximum})")

    @property
    def user_message(self) -> str:
        """返回不包含内容的稳定业务提示。"""
        labels = {
            "file_bytes": "单个记忆文件不能超过",
            "files": "记忆文件数量不能超过",
            "total_bytes": "记忆总容量不能超过",
        }
        return f"{labels.get(self.limit, '记忆容量不能超过')} {self.maximum}"


class AgentMemoryConflict(ValueError):
    """文件已变化或目标重名，不能覆盖其他用户操作。"""


class AgentMemoryIdentity(BaseModel):
    """账号和 Agent 组成的长期记忆隔离身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: UUID
    app_id: UUID

    @property
    def namespace(self) -> tuple[str, ...]:
        """返回 LangGraph Store 使用的稳定命名空间。"""
        return (
            *AGENT_MEMORY_NAMESPACE_ROOT,
            str(self.account_id),
            str(self.app_id),
        )


class AgentMemoryLimits(BaseModel):
    """单个记忆作用域的容量上限。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_file_bytes: int = Field(default=64 * 1024, gt=0, strict=True)
    max_files: int = Field(default=100, gt=0, strict=True)
    max_total_bytes: int = Field(default=1024 * 1024, gt=0, strict=True)


DEFAULT_AGENT_MEMORY_LIMITS = AgentMemoryLimits()


class AgentMemoryFile(BaseModel):
    """HTTP 管理接口与 Store Repository 之间的记忆文件实体。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=MAX_AGENT_MEMORY_PATH_LENGTH)
    content: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """确保 Repository 返回的文件仍位于记忆命名空间。"""
        return normalize_agent_memory_path(value)

    @property
    def size_bytes(self) -> int:
        """返回 UTF-8 编码后的实际字节数。"""
        return len(self.content.encode("utf-8"))

    @property
    def content_sha256(self) -> str:
        """用于自动保存的并发校验，不保存修订历史。"""
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def preview(self) -> str:
        """列表只返回简短摘要，正文通过读取接口获取。"""
        return " ".join(self.content.split())[:100]


def normalize_agent_memory_path(path: str) -> str:
    """校验并规范 Agent 可见的 `/memories/` 虚拟路径。"""
    normalized = (path or "").strip().replace("\\", "/")
    if not normalized.startswith(AGENT_MEMORY_VIRTUAL_ROOT):
        raise AgentMemoryPathError("记忆文件路径必须以 /memories/ 开头")
    relative = normalized[len(AGENT_MEMORY_VIRTUAL_ROOT) :].strip("/")
    relative = _normalize_relative_path(relative, original=normalized)
    return f"{AGENT_MEMORY_VIRTUAL_ROOT}{relative}"


def memory_virtual_path_to_store_key(path: str) -> str:
    """把 Agent 虚拟路径转换为 CompositeBackend 实际保存的 Store key。"""
    normalized = normalize_agent_memory_path(path)
    return f"/{normalized[len(AGENT_MEMORY_VIRTUAL_ROOT):]}"


def memory_store_key_to_virtual_path(key: str) -> str:
    """把 StoreBackend 的相对 key 恢复为 Agent 虚拟路径。"""
    normalized = (key or "").strip().replace("\\", "/")
    relative = normalized.strip("/")
    relative = _normalize_relative_path(
        relative,
        original=f"{AGENT_MEMORY_VIRTUAL_ROOT}{relative}",
    )
    return f"{AGENT_MEMORY_VIRTUAL_ROOT}{relative}"


def validate_agent_memory_content(
    content: str,
    *,
    limits: AgentMemoryLimits = DEFAULT_AGENT_MEMORY_LIMITS,
) -> int:
    """校验记忆仅为小体量 UTF-8 文本，并返回字节数。"""
    if not isinstance(content, str):
        raise TypeError("记忆文件内容必须是字符串")
    size = len(content.encode("utf-8"))
    if size > limits.max_file_bytes:
        raise AgentMemoryQuotaExceeded("file_bytes", limits.max_file_bytes)
    return size


def validate_agent_memory_scope(
    *,
    file_count: int,
    total_bytes: int,
    limits: AgentMemoryLimits = DEFAULT_AGENT_MEMORY_LIMITS,
) -> None:
    """校验一个主体的记忆文件数和总容量。"""
    if file_count > limits.max_files:
        raise AgentMemoryQuotaExceeded("files", limits.max_files)
    if total_bytes > limits.max_total_bytes:
        raise AgentMemoryQuotaExceeded("total_bytes", limits.max_total_bytes)


def _normalize_relative_path(relative: str, *, original: str) -> str:
    """校验并规范不含根前缀的 Store 相对路径。"""
    segments = PurePosixPath(relative).parts if relative else ()
    if (
        not segments
        or len(original) > MAX_AGENT_MEMORY_PATH_LENGTH
        or "\x00" in original
        or any(
            segment in {"", ".", "..", "~"}
            or any(ord(character) < 32 for character in segment)
            for segment in segments
        )
    ):
        raise AgentMemoryPathError("记忆文件路径不合法")
    return "/".join(segments)


__all__ = [
    "AGENT_MEMORY_NAMESPACE_ROOT",
    "AGENT_MEMORY_VIRTUAL_ROOT",
    "AgentMemoryFile",
    "AgentMemoryIdentity",
    "AgentMemoryLimits",
    "AgentMemoryPathError",
    "AgentMemoryQuotaExceeded",
    "DEFAULT_AGENT_MEMORY_LIMITS",
    "memory_store_key_to_virtual_path",
    "memory_virtual_path_to_store_key",
    "normalize_agent_memory_path",
    "validate_agent_memory_content",
    "validate_agent_memory_scope",
]
