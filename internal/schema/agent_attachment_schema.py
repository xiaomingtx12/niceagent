"""Agent 附件响应 Schema。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from internal.entity.agent_attachment_entity import AgentAttachment


class AgentAttachmentResp(BaseModel):
    """可持久化并传给模型适配层的附件响应。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    path: str
    url: str
    size: int
    extension: str
    mime_type: str

    @classmethod
    def from_entity(cls, attachment: AgentAttachment) -> "AgentAttachmentResp":
        """把附件领域对象投影为稳定响应。"""
        return cls(
            id=attachment.id,
            name=attachment.name,
            path=attachment.path,
            url=attachment.url,
            size=attachment.size,
            extension=attachment.extension,
            mime_type=attachment.mime_type,
        )

    def dump(self) -> dict[str, Any]:
        """使用 JSON 模式输出稳定的协议字典。"""
        return self.model_dump(mode="json")


def dump_agent_attachments(
    attachments: Sequence[AgentAttachment],
) -> list[dict[str, Any]]:
    """批量投影附件响应并保持提交顺序。"""
    return [AgentAttachmentResp.from_entity(item).dump() for item in attachments]


__all__ = [
    "AgentAttachmentResp",
    "dump_agent_attachments",
]
