"""Agent 附件的归属校验、COS 读取和 Workspace 投影。"""

from __future__ import annotations

import mimetypes
from collections.abc import Sequence
from dataclasses import dataclass

from deepagents.backends.protocol import BackendProtocol
from injector import inject

from internal.entity.agent_attachment_entity import AgentAttachment
from internal.exception import FailException, ForbiddenException
from internal.model import UploadFile
from internal.schema.agent_attachment_schema import AgentAttachmentResolveReq
from pkg.sqlalchemy import SQLAlchemy
from .cos_service import CosService


@inject
@dataclass
class AgentAttachmentService:
    """解析用户附件并将其投影到当前 Agent Workspace。

    文件归属和 COS 读取属于 Service；Backend 只作为 Agent Core 提供的
    文件写入协议传入，不由本服务管理 Agent 生命周期。
    """

    db: SQLAlchemy
    cos_service: CosService

    def resolve(
        self,
        req: AgentAttachmentResolveReq,
    ) -> list[AgentAttachment]:
        """按提交顺序校验上传记录归属，并返回可信附件元数据。"""
        if not req.file_ids:
            return []

        # 1.按当前用户查询上传记录，阻断跨用户读取。
        rows = (
            self.db.session.query(UploadFile)
            .filter(
                UploadFile.id.in_(req.file_ids),
                UploadFile.account_id == req.owner_id,
            )
            .all()
        )
        rows_by_id = {row.id: row for row in rows}
        missing = [item for item in req.file_ids if item not in rows_by_id]
        if missing:
            raise ForbiddenException("附件不存在或不属于当前用户")

        # 2.按请求顺序转换为 Agent 可见的可信领域对象。
        return [
            AgentAttachment(
                id=row.id,
                name=row.name,
                key=row.key,
                url=self.cos_service.get_file_url(row.key),
                size=int(row.size or 0),
                extension=row.extension,
                mime_type=(
                    row.mime_type
                    or mimetypes.guess_type(row.name)[0]
                    or "application/octet-stream"
                ),
            )
            for item in req.file_ids
            for row in [rows_by_id[item]]
        ]

    def materialize(
        self,
        attachments: Sequence[AgentAttachment],
        backend: BackendProtocol,
    ) -> None:
        """下载附件内容并写入当前 Run 的 Workspace 路由。"""
        if not attachments:
            return
        # 1.从对象存储下载附件内容，并映射为 Workspace 虚拟路径。
        files: list[tuple[str, bytes]] = [
            (attachment.path, self.cos_service.download_file_bytes(attachment.key))
            for attachment in attachments
        ]
        # 2.批量上传到本次运行的 Backend。
        responses = list(backend.upload_files(files))
        # Backend 契约要求结果与输入一一对应，缺失结果也必须按失败处理。
        if len(responses) != len(files) or any(
            response.error for response in responses
        ):
            raise FailException("附件写入 Agent Workspace 失败")


__all__ = ["AgentAttachmentService"]
