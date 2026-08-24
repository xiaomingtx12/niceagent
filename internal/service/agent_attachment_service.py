"""Agent 附件的归属校验、COS 读取和 Workspace 投影。"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Sequence
from uuid import UUID

from deepagents.backends.protocol import BackendProtocol
from injector import inject

from internal.exception import FailException, ForbiddenException
from internal.model import UploadFile
from pkg.sqlalchemy import SQLAlchemy
from .cos_service import CosService

MAX_AGENT_ATTACHMENTS = 5


@dataclass(frozen=True, slots=True)
class AgentAttachment:
    """一次 Agent 调用可见的附件元数据。"""

    id: UUID
    name: str
    key: str
    size: int
    extension: str
    mime_type: str

    @property
    def path(self) -> str:
        """返回会话 Workspace 中的稳定路径。"""
        suffix = self.extension.lower().strip(".")
        return f"/workspace/attachments/{self.id}{('.' + suffix) if suffix else ''}"

    @property
    def url(self) -> str:
        """返回附件的 COS 访问地址。"""
        return CosService.get_file_url(self.key)

    @property
    def media_type(self) -> str:
        """返回 MIME 主类型。"""
        return (self.mime_type or "").split("/", 1)[0].lower()

    def to_dict(self) -> dict[str, object]:
        """返回可持久化和传给模型适配层的安全元数据。"""
        return {
            "id": str(self.id),
            "name": self.name,
            "path": self.path,
            "url": self.url,
            "size": self.size,
            "extension": self.extension,
            "mime_type": self.mime_type,
        }


@inject
@dataclass
class AgentAttachmentService:
    """解析可信上传记录，并把附件投影到 DeepAgents Backend。"""

    db: SQLAlchemy
    cos_service: CosService

    def resolve(
        self,
        file_ids: Sequence[str | UUID] | None,
        *,
        owner_id: UUID,
    ) -> list[AgentAttachment]:
        """按提交顺序校验附件归属并返回元数据。"""
        # 1.限制附件数量，并保留客户端提交顺序。
        raw_ids = list(file_ids or [])
        if len(raw_ids) > MAX_AGENT_ATTACHMENTS:
            raise FailException(
                f"一次对话最多只能携带 {MAX_AGENT_ATTACHMENTS} 个附件"
            )

        # 2.规范化 UUID，并拒绝重复附件。
        normalized: list[UUID] = []
        for value in raw_ids:
            try:
                normalized.append(UUID(str(value)))
            except (TypeError, ValueError) as exc:
                raise FailException("附件 id 必须是合法 UUID") from exc

        if len(normalized) != len(set(normalized)):
            raise FailException("附件不能重复提交")
        if not normalized:
            return []

        # 3.按当前用户查询上传记录，阻断跨用户读取。
        rows = (
            self.db.session.query(UploadFile)
            .filter(
                UploadFile.id.in_(normalized),
                UploadFile.account_id == owner_id,
            )
            .all()
        )
        rows_by_id = {row.id: row for row in rows}
        missing = [item for item in normalized if item not in rows_by_id]
        if missing:
            raise ForbiddenException("附件不存在或不属于当前用户")

        # 4.按原始顺序转换为 Agent 可见的安全元数据。
        return [
            AgentAttachment(
                id=row.id,
                name=row.name,
                key=row.key,
                size=int(row.size or 0),
                extension=row.extension or PurePosixPath(row.name).suffix.lstrip("."),
                mime_type=(
                    row.mime_type
                    or mimetypes.guess_type(row.name)[0]
                    or "application/octet-stream"
                ),
            )
            for item in normalized
            for row in [rows_by_id[item]]
        ]

    def materialize(
        self,
        attachments: Sequence[AgentAttachment],
        backend: BackendProtocol,
    ) -> None:
        """把附件字节写入当前运行的 Workspace 路由。"""
        if not attachments:
            return
        # 1.从对象存储下载附件内容，并映射为 Workspace 虚拟路径。
        files = [
            (attachment.path, self.cos_service.download_file_bytes(attachment.key))
            for attachment in attachments
        ]
        # 2.批量上传到本次运行的 Backend。
        responses = backend.upload_files(files)
        # 3.检查每个上传结果，失败时阻止 Agent 继续执行。
        failed = next((response for response in responses if response.error), None)
        if failed is not None:
            raise FailException(f"附件写入 Agent Workspace 失败: {failed.error}")


__all__ = [
    "AgentAttachment",
    "AgentAttachmentService",
    "MAX_AGENT_ATTACHMENTS",
]
