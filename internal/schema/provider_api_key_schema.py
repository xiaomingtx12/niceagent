from flask_wtf import FlaskForm
from marshmallow import Schema, fields, pre_dump
from wtforms import BooleanField, StringField, ValidationError
from wtforms.validators import Length, DataRequired, Optional, URL, AnyOf

from internal.lib.helper import datetime_to_timestamp
from internal.model.provider_api_key import ProviderApiKey
from .schema import JsonBoolean, JsonString, ListField


class CreateProviderApiKeyReq(FlaskForm):
    """创建提供商API密钥请求"""

    connection_name = StringField("connection_name", validators=[JsonString("服务名称必须是字符串"), DataRequired("服务名称不能为空"), Length(max=255)])
    api_format = StringField("api_format", validators=[JsonString("接口格式必须是字符串"), DataRequired("接口格式不能为空"), AnyOf(["chat_completions", "responses"])])
    provider_name = StringField(
        "provider_name",
        validators=[
            JsonString("提供商名称必须是字符串"),
            DataRequired(message="提供商名称不能为空"),
            Length(max=255, message="提供商名称不能超过255个字符"),
        ],
    )
    api_key = StringField(
        "api_key",
        validators=[
            JsonString("API密钥必须是字符串"),
            DataRequired(message="API密钥不能为空"),
            Length(max=500, message="API密钥不能超过500个字符"),
        ],
    )
    base_url = StringField(
        "base_url",
        validators=[
            JsonString("模型服务地址必须是字符串"),
            Optional(),
            URL(message="模型服务地址必须是 http 或 https URL"),
            Length(max=1000, message="模型服务地址不能超过1000个字符"),
        ],
    )
    is_active = BooleanField(
        "is_active",
        default=True,
        validators=[JsonBoolean("启用状态必须是布尔值")],
    )
    discovered_models = ListField("discovered_models")
    enabled_models = ListField("enabled_models")

    def validate_discovered_models(self, field: ListField) -> None:
        """发现模型必须是对象数组，未提供时使用空数组。"""
        if field.data is None:
            field.data = []
        if not isinstance(field.data, list) or not all(
            isinstance(model, dict) for model in field.data
        ):
            raise ValidationError("发现模型必须是对象数组")

    def validate_enabled_models(self, field: ListField) -> None:
        """激活模型可省略，提供时必须是字符串数组。"""
        if field.data is not None and (
            not isinstance(field.data, list)
            or not all(isinstance(model_name, str) for model_name in field.data)
        ):
            raise ValidationError("激活模型必须是字符串数组")


class UpdateProviderApiKeyReq(FlaskForm):
    """更新提供商API密钥请求"""

    connection_name = StringField("connection_name", validators=[JsonString("服务名称必须是字符串"), Optional(), Length(max=255)])
    provider_name = StringField("provider_name", validators=[JsonString("提供商必须是字符串"), Optional(), Length(max=255)])
    api_format = StringField("api_format", validators=[JsonString("接口格式必须是字符串"), Optional(), AnyOf(["chat_completions", "responses"])])
    discovered_models = ListField("discovered_models")
    enabled_models = ListField("enabled_models")
    validate_enabled_models = CreateProviderApiKeyReq.validate_enabled_models

    def validate_discovered_models(self, field: ListField) -> None:
        """可选快照与连接信息在同一次保存中提交。"""
        if field.data is not None and (not isinstance(field.data, list) or not all(isinstance(model, dict) for model in field.data)):
            raise ValidationError("发现模型必须是对象数组")

    api_key = StringField(
        "api_key",
        validators=[
            JsonString("API密钥必须是字符串"),
            Length(max=500, message="API密钥不能超过500个字符"),
        ],
    )
    base_url = StringField(
        "base_url",
        validators=[
            JsonString("模型服务地址必须是字符串"),
            Optional(),
            URL(message="模型服务地址必须是 http 或 https URL"),
            Length(max=1000, message="模型服务地址不能超过1000个字符"),
        ],
    )
    is_active = BooleanField(
        "is_active",
        validators=[JsonBoolean("启用状态必须是布尔值", allow_none=True)],
    )


class UpdateProviderApiKeyModelsReq(FlaskForm):
    """更新供应商动态模型激活状态请求。"""

    enabled_models = ListField("enabled_models")
    discovered_models = ListField("discovered_models")

    def validate_enabled_models(self, field: ListField) -> None:
        """激活模型必须显式提供字符串数组，空数组表示全部停用。"""
        if not isinstance(field.data, list) or not all(
            isinstance(model_name, str) for model_name in field.data
        ):
            raise ValidationError("已激活模型必须是字符串数组")

    def validate_discovered_models(self, field: ListField) -> None:
        """发现模型可省略，提供时必须是对象数组。"""
        if field.data is not None and (
            not isinstance(field.data, list)
            or not all(isinstance(model, dict) for model in field.data)
        ):
            raise ValidationError("发现模型必须是对象数组")


class GetProviderApiKeysResp(Schema):
    """获取提供商API密钥列表响应"""

    id = fields.UUID(dump_default="")
    provider_name = fields.String(dump_default="")
    api_key_masked = fields.String(dump_default="")  # 脱敏后的密钥
    base_url = fields.String(dump_default="")
    connection_name = fields.String(dump_default="")
    api_format = fields.String()
    is_active = fields.Boolean(dump_default=True)
    updated_at = fields.Integer(dump_default=0)
    created_at = fields.Integer(dump_default=0)
    discovered_model_count = fields.Integer(dump_default=0)
    enabled_model_count = fields.Integer(dump_default=0)
    enabled_models = fields.List(fields.String(), dump_default=list)
    discovered_models = fields.List(
        fields.Dict(), dump_default=list
    )

    @pre_dump
    def process_data(self, data: ProviderApiKey, **kwargs):
        # 对API密钥进行脱敏处理，只显示前8位和后4位
        api_key = data.api_key
        if len(api_key) > 12:
            api_key_masked = api_key[:8] + "*" * (len(api_key) - 12) + api_key[-4:]
        else:
            api_key_masked = "*" * len(api_key)

        return {
            "id": data.id,
            "provider_name": data.provider_name,
            "api_key_masked": api_key_masked,
            "base_url": data.base_url or "",
            "connection_name": data.connection_name,
            "api_format": data.api_format,
            "is_active": data.is_active,
            "updated_at": datetime_to_timestamp(data.updated_at),
            "created_at": datetime_to_timestamp(data.created_at),
            "discovered_model_count": len(data.discovered_models or []),
            "enabled_model_count": len(data.enabled_models or []),
            "enabled_models": data.enabled_models or [],
            "discovered_models": data.discovered_models or [],
        }
