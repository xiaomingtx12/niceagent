from flask_wtf import FlaskForm
from wtforms import BooleanField, StringField, ValidationError
from wtforms.validators import DataRequired, Length, Optional, UUID

from pkg.paginator import CursorPaginatorReq
from .schema import JsonString


class GetCreatorTaskConversationsReq(CursorPaginatorReq):
    """Creator 任务会话列表请求。"""

    app_id = StringField(
        "app_id",
        validators=[Optional(), UUID(message="app_id必须是合法的UUID")],
    )
    search = StringField(
        "search",
        default="",
        validators=[Optional(), Length(max=255, message="搜索内容不能超过255个字符")],
    )


class UpdateCreatorTaskConversationNameReq(FlaskForm):
    """Creator 任务会话名称更新请求。"""

    name = StringField(
        "name",
        validators=[
            JsonString("会话名称必须是字符串"),
            DataRequired(message="会话名称不能为空"),
            Length(max=255, message="会话名称不能超过255个字符"),
        ],
    )


class UpdateCreatorTaskConversationPinnedReq(FlaskForm):
    """Creator 任务会话置顶状态更新请求。"""

    is_pinned = BooleanField("is_pinned")

    def validate_is_pinned(self, field: BooleanField) -> None:
        """布尔字段必须由请求显式提供。"""
        if not field.raw_data or not isinstance(field.raw_data[0], bool):
            raise ValidationError("is_pinned必须是布尔值")


__all__ = [
    "GetCreatorTaskConversationsReq",
    "UpdateCreatorTaskConversationNameReq",
    "UpdateCreatorTaskConversationPinnedReq",
]
