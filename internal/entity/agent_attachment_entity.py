"""Agent 附件领域值对象。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID

_SAFE_EXTENSION_PATTERN = re.compile(r"[a-z0-9][a-z0-9.+-]{0,15}")


def normalize_agent_attachment_extension(
    extension: str | None,
    filename: str,
) -> str:
    """返回可安全拼接到 Workspace 路径的扩展名。"""
    candidates = (extension, PurePosixPath(filename).suffix.lstrip("."))
    for candidate in candidates:
        normalized = str(candidate or "").strip().lower().lstrip(".")
        if _SAFE_EXTENSION_PATTERN.fullmatch(normalized):
            return normalized
    return ""


@dataclass(frozen=True, slots=True)
class AgentAttachment:
    """一次 Agent 调用可见的可信附件元数据。"""

    id: UUID
    name: str
    key: str
    url: str
    size: int
    extension: str
    mime_type: str

    def __post_init__(self) -> None:
        """冻结安全扩展名，避免后续路径投影出现目录穿越。"""
        object.__setattr__(
            self,
            "extension",
            normalize_agent_attachment_extension(self.extension, self.name),
        )

    @property
    def path(self) -> str:
        """返回会话 Workspace 中的稳定路径。"""
        suffix = f".{self.extension}" if self.extension else ""
        return f"/workspace/attachments/{self.id}{suffix}"

    @property
    def media_type(self) -> str:
        """返回 MIME 主类型。"""
        return (self.mime_type or "").split("/", 1)[0].lower()


__all__ = ["AgentAttachment", "normalize_agent_attachment_extension"]
