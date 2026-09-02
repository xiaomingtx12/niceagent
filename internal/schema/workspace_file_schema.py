"""Workspace 文件 HTTP 请求和响应 Schema。"""

from marshmallow import Schema, fields, pre_dump
from flask import request
from wtforms import StringField
from wtforms.validators import DataRequired, Length, Optional, UUID, ValidationError

from flask_wtf import FlaskForm

from internal.entity.workspace_file_entity import WorkspaceFilePreview
from internal.model import WorkspaceFile


class ListWorkspaceFilesReq(FlaskForm):
    """列出当前用户 Agent Workspace 文件请求。"""

    conversation_id = StringField(
        "conversation_id",
        validators=[Optional(), UUID()],
    )
    path_prefix = StringField("path_prefix", validators=[Optional(), Length(max=512)])


class SearchWorkspaceFilesReq(FlaskForm):
    """终端用户跨会话搜索 Workspace 文件请求。"""

    query = StringField("query", validators=[Optional(), Length(max=200)])


class ReadWorkspaceFileReq(FlaskForm):
    """读取当前用户 Agent Workspace 文件请求。"""

    conversation_id = StringField(
        "conversation_id",
        validators=[Optional(), UUID()],
    )
    path = StringField(
        "path",
        validators=[DataRequired("path 不能为空"), Length(max=512)],
    )


class ConversationWorkspaceFileReq(FlaskForm):
    """已由路由确定会话的 Workspace 文件请求。"""

    path = StringField(
        "path",
        validators=[DataRequired("path 不能为空"), Length(max=512)],
    )


class WriteWorkspaceFileReq(ReadWorkspaceFileReq):
    """写入 Workspace 文本文件请求。"""

    content = StringField("content")
    mime_type = StringField(
        "mime_type",
        default="text/plain",
        validators=[Optional(), Length(max=128)],
    )

    def validate_content(self, field: StringField) -> None:
        """允许空文件，但不允许缺少 content 字段。"""
        payload = request.get_json(silent=True) or {}
        if "content" not in payload or not isinstance(payload["content"], str):
            raise ValidationError("content 必须是字符串")


class DeleteWorkspaceFileReq(ReadWorkspaceFileReq):
    """删除 Workspace 文件请求。"""


class WorkspaceFileResp(Schema):
    """Workspace 文件元数据响应。"""

    id = fields.UUID(required=True)
    app_id = fields.UUID(required=True)
    conversation_id = fields.UUID(required=True)
    path = fields.String(required=True)
    revision = fields.Integer(required=True)
    mime_type = fields.String(required=True)
    size_bytes = fields.Integer(required=True)
    content_sha256 = fields.String(required=True)
    created_by = fields.UUID(allow_none=True)
    created_at = fields.DateTime(allow_none=True)
    updated_at = fields.DateTime(allow_none=True)

    @pre_dump
    def project_file(self, row: WorkspaceFile, **kwargs):
        """把 ORM 显式投影为稳定响应字段。"""
        return {
            "id": row.id,
            "app_id": row.app_id,
            "conversation_id": row.conversation_id,
            "path": row.virtual_path,
            "revision": row.revision,
            "mime_type": row.mime_type,
            "size_bytes": row.size_bytes,
            "content_sha256": row.content_sha256,
            "created_by": row.created_by,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


class WorkspaceFilePreviewResp(WorkspaceFileResp):
    """Workspace 文件预览响应。"""

    content = fields.String(allow_none=True)
    download_url = fields.String(allow_none=True)
    truncated = fields.Boolean(required=True)

    @pre_dump
    def project_file(self, preview: WorkspaceFilePreview, **kwargs):
        """合并文件元数据和预览载荷。"""
        data = WorkspaceFileResp.project_file(self, preview.file, **kwargs)
        data.update(
            content=preview.content,
            download_url=preview.download_url,
            truncated=preview.truncated,
        )
        return data


class WorkspaceFileWithConversationResp(WorkspaceFileResp):
    """附带会话名称的 Workspace 文件元数据响应。"""

    conversation_name = fields.String(required=True)

    @pre_dump
    def project_file(self, item: tuple[WorkspaceFile, str], **kwargs):
        """把文件与 Service 查询出的会话名称合并。"""
        row, conversation_name = item
        data = WorkspaceFileResp.project_file(self, row, **kwargs)
        data["conversation_name"] = conversation_name
        return data


__all__ = [
    "ConversationWorkspaceFileReq",
    "DeleteWorkspaceFileReq",
    "ListWorkspaceFilesReq",
    "ReadWorkspaceFileReq",
    "SearchWorkspaceFilesReq",
    "WorkspaceFilePreviewResp",
    "WorkspaceFileResp",
    "WorkspaceFileWithConversationResp",
    "WriteWorkspaceFileReq",
]
