from urllib.parse import urlparse

from flask_wtf import FlaskForm
from wtforms import StringField, ValidationError
from wtforms.validators import DataRequired, Length, Optional, AnyOf, UUID

from internal.core.language_model.entities.model_entity import ModelType
from internal.schema.common.schema import JsonString


class GetLanguageModelsReq(FlaskForm):
    """语言模型列表筛选请求。"""

    model_type = StringField("model_type", validators=[Optional()])

    def validate_model_type(self, field: StringField) -> None:
        """规范化逗号分隔的模型类型，并拒绝未知类型。"""
        values = [
            value.strip()
            for value in (field.data or "").split(",")
            if value.strip()
        ]
        try:
            field.data = {ModelType(value) for value in values} if values else None
        except ValueError as exc:
            supported = ", ".join(model_type.value for model_type in ModelType)
            raise ValidationError(f"模型类型必须是: {supported}") from exc


class DiscoverLanguageModelsReq(FlaskForm):
    """第三方语言模型发现请求。"""

    provider_connection_id = StringField("provider_connection_id", validators=[JsonString("连接ID必须是字符串"), Optional(), UUID()])
    api_format = StringField("api_format", default="chat_completions", validators=[JsonString("接口格式必须是字符串"), AnyOf(["chat_completions", "responses"])])
    provider_name = StringField(
        "provider_name",
        validators=[
            JsonString("提供商名称必须是字符串"),
            DataRequired("提供商名称不能为空"),
            Length(max=255),
        ],
    )
    api_key = StringField(
        "api_key",
        validators=[
            JsonString("API密钥必须是字符串"),
            Length(max=500),
        ],
    )
    base_url = StringField(
        "base_url",
        validators=[
            JsonString("模型服务地址必须是字符串"),
            Optional(),
            Length(max=1000),
        ],
    )

    def validate_base_url(self, field: StringField) -> None:
        """模型服务地址只接受带主机名的 HTTP(S) URL。"""
        if not field.data:
            return
        parsed = urlparse(field.data.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValidationError("模型服务地址必须是 http 或 https URL")


__all__ = ["DiscoverLanguageModelsReq", "GetLanguageModelsReq"]
