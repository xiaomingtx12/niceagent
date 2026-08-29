"""长期记忆文件的 HTTP 请求和响应 Schema。"""

from __future__ import annotations

from flask import request
from flask_wtf import FlaskForm
from marshmallow import Schema, fields, pre_dump
from wtforms import StringField
from wtforms.validators import DataRequired, Length, ValidationError

from internal.core.agent_v2.entity.agent_memory import AgentMemoryFile


class ReadMemoryFileReq(FlaskForm):
    """读取长期记忆文件请求。"""

    path = StringField(
        "path",
        validators=[DataRequired("path 不能为空"), Length(max=512)],
    )


class WriteMemoryFileReq(ReadMemoryFileReq):
    """写入长期记忆文本请求。"""

    content = StringField("content")

    def validate_content(self, field: StringField) -> None:
        """允许空文本，但不允许缺少字段或传入非字符串。"""
        payload = request.get_json(silent=True) or {}
        if "content" not in payload or not isinstance(payload["content"], str):
            raise ValidationError("content 必须是字符串")


class DeleteMemoryFileReq(ReadMemoryFileReq):
    """删除长期记忆文件请求。"""


class MemoryFileResp(Schema):
    """长期记忆文件元数据响应。"""

    path = fields.String(required=True)
    size_bytes = fields.Integer(required=True)
    created_at = fields.DateTime(allow_none=True)
    updated_at = fields.DateTime(allow_none=True)

    @pre_dump
    def project_file(self, row: AgentMemoryFile, **kwargs):
        """显式投影 Entity，避免暴露 Store 内部字段。"""
        return {
            "path": row.path,
            "size_bytes": row.size_bytes,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


class MemoryFileContentResp(MemoryFileResp):
    """包含 UTF-8 文本内容的长期记忆文件响应。"""

    content = fields.String(required=True)

    @pre_dump
    def project_file(self, row: AgentMemoryFile, **kwargs):
        """在元数据响应上追加文本内容。"""
        data = MemoryFileResp.project_file(self, row, **kwargs)
        data["content"] = row.content
        return data


class ListMemoryFilesResp(Schema):
    """长期记忆文件列表响应。"""

    items = fields.List(fields.Nested(MemoryFileResp), required=True)


__all__ = [
    "DeleteMemoryFileReq",
    "ListMemoryFilesResp",
    "MemoryFileContentResp",
    "MemoryFileResp",
    "ReadMemoryFileReq",
    "WriteMemoryFileReq",
]
