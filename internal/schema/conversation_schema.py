from flask_wtf import FlaskForm
from marshmallow import Schema, fields, pre_dump
from wtforms import IntegerField, StringField, BooleanField
from wtforms.validators import Optional, NumberRange, DataRequired, Length

from internal.lib.helper import datetime_to_timestamp
from internal.model import Message
from pkg.paginator import PaginatorReq


class GetConversationMessagesWithPageReq(PaginatorReq):
    """获取指定会话消息列表分页数据请求结构"""

    created_at = IntegerField(
        "created_at",
        default=0,
        validators=[Optional(), NumberRange(min=0, message="created_at游标最小值为0")],
    )


class GetConversationMessagesWithPageResp(Schema):
    """获取指定会话消息列表分页数据响应结构"""

    id = fields.UUID(dump_default="")
    conversation_id = fields.UUID(dump_default="")
    query = fields.String(dump_default="")
    image_urls = fields.List(fields.String, dump_default=[])
    attachments = fields.List(fields.Dict, dump_default=[])
    answer = fields.String(dump_default="")
    total_token_count = fields.Integer(dump_default=0)
    input_token_count = fields.Integer(dump_default=0)
    output_token_count = fields.Integer(dump_default=0)
    cached_token_count = fields.Integer(dump_default=0)
    reasoning_token_count = fields.Integer(dump_default=0)
    model_call_count = fields.Integer(dump_default=0)
    tool_call_count = fields.Integer(dump_default=0)
    trace_id = fields.String(allow_none=True, dump_default=None)
    token_usage = fields.Dict(dump_default={})
    latency = fields.Float(dump_default=0)
    created_at = fields.Integer(dump_default=0)

    @pre_dump
    def process_data(self, data: Message, **kwargs):
        return {
            "id": data.id,
            "conversation_id": data.conversation_id,
            "query": data.query,
            "image_urls": data.image_urls,
            "attachments": data.attachments,
            "answer": data.answer,
            "total_token_count": data.total_token_count,
            "input_token_count": data.input_token_count,
            "output_token_count": data.output_token_count,
            "cached_token_count": data.cached_token_count,
            "reasoning_token_count": data.reasoning_token_count,
            "model_call_count": data.model_call_count,
            "tool_call_count": data.tool_call_count,
            "trace_id": data.trace_id,
            "token_usage": data.token_usage,
            "latency": data.latency,
            "created_at": datetime_to_timestamp(data.created_at),
        }


class UpdateConversationNameReq(FlaskForm):
    """更新会话名字请求结构体"""

    name = StringField(
        "name",
        validators=[
            DataRequired(message="会话名字不能为空"),
            Length(max=100, message="会话名字长度不能超过100个字符"),
        ],
    )


class UpdateConversationIsPinnedReq(FlaskForm):
    """更新会话置顶选项请求请求结构体"""

    is_pinned = BooleanField("is_pinned", default=False)
