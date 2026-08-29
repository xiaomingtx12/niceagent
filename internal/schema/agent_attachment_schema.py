"""Agent 附件请求与响应 Schema。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from internal.entity.agent_attachment_entity import AgentAttachment
from internal.exception import ValidateErrorException

_MAX_AGENT_ATTACHMENTS = 5


class AgentAttachmentResolveReq(BaseModel):
    """解析一次 Agent 调用附件所需的可信请求。"""

    model_config = ConfigDict(extra="forbid")

    owner_id: UUID
    file_ids: list[UUID] = Field(
        default_factory=list,
        max_length=_MAX_AGENT_ATTACHMENTS,
    )

    @field_validator("file_ids")
    @classmethod
    def reject_duplicate_file_ids(cls, value: list[UUID]) -> list[UUID]:
        """拒绝同一附件被重复提交。"""
        if len(value) != len(set(value)):
            raise ValueError("附件不能重复提交")
        return value

    @classmethod
    def from_values(
        cls,
        file_ids: Sequence[str | UUID] | None,
        *,
        owner_id: UUID,
    ) -> "AgentAttachmentResolveReq":
        """解析调用方值并转换为项目统一校验异常。"""
        try:
            return cls.model_validate(
                {"owner_id": owner_id, "file_ids": list(file_ids or [])}
            )
        except ValidationError as exc:
            raise ValidateErrorException(
                "附件参数校验失败",
                data=json.loads(
                    exc.json(
                        include_url=False,
                        include_context=False,
                    )
                ),
            ) from exc


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
    "AgentAttachmentResolveReq",
    "AgentAttachmentResp",
    "dump_agent_attachments",
]
