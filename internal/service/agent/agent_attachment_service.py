"""Agent 附件的归属校验、COS 读取和 Workspace 投影。"""

from __future__ import annotations

import mimetypes
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from injector import inject

from internal.entity.agent_attachment_entity import AgentAttachment
from internal.exception import ForbiddenException
from internal.model import UploadFile
from pkg.sqlalchemy import SQLAlchemy
from internal.service.file.cos_service import CosService


@inject
@dataclass
class AgentAttachmentService:
    """解析已授权附件并从 COS 读取内容，本轮工作副本的写入由 Core 装配器负责。"""

    db: SQLAlchemy
    cos_service: CosService

    def resolve(
        self,
        file_ids: Sequence[UUID],
        *,
        owner_id: UUID,
    ) -> tuple[AgentAttachment, ...]:
        """按提交顺序校验上传记录归属，并返回可信附件元数据。"""
        if not file_ids:
            return ()

        # 1.按当前用户查询上传记录，阻断跨用户读取。
        rows = (
            self.db.session.query(UploadFile)
            .filter(
                UploadFile.id.in_(file_ids),
                UploadFile.account_id == owner_id,
            )
            .all()
        )
        rows_by_id = {row.id: row for row in rows}
        missing = [file_id for file_id in file_ids if file_id not in rows_by_id]
        if missing:
            raise ForbiddenException("附件不存在或不属于当前用户")

        # 2.按请求顺序转换为 Agent 可见的可信领域对象。
        attachments = []
        for file_id in file_ids:
            row = rows_by_id[file_id]
            attachment = AgentAttachment(
                id=row.id,
                name=row.name,
                key=row.key,
                url=self.cos_service.get_file_url(row.key),
                size=int(row.size or 0),
                extension=row.extension,
                mime_type=row.mime_type
                or mimetypes.guess_type(row.name)[0]
                or "application/octet-stream",
            )
            attachments.append(attachment)
        return tuple(attachments)

    def load_files(
        self,
        attachments: Sequence[AgentAttachment],
    ) -> list[tuple[str, bytes]]:
        """返回已授权附件的虚拟路径和内容，失败发生在本轮资源创建之前。"""
        if not attachments:
            return []
        # 1.从对象存储下载附件内容，并映射为 Workspace 虚拟路径。
        files: list[tuple[str, bytes]] = []
        for attachment in attachments:
            content = self.cos_service.download_file_bytes(attachment.key)
            files.append((attachment.path, content))
        return files


__all__ = ["AgentAttachmentService"]
