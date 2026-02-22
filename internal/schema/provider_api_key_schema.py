from flask_wtf import FlaskForm
from marshmallow import Schema, fields, pre_dump
from wtforms import BooleanField, StringField
from wtforms.validators import Length, DataRequired

from internal.lib.helper import datetime_to_timestamp
from internal.model.provider_api_key import ProviderApiKey


class CreateProviderApiKeyReq(FlaskForm):
    """创建提供商API密钥请求"""

    provider_name = StringField(
        "provider_name",
        validators=[
            DataRequired(message="提供商名称不能为空"),
            Length(max=255, message="提供商名称不能超过255个字符"),
        ],
    )
    api_key = StringField(
        "api_key",
        validators=[
            DataRequired(message="API密钥不能为空"),
            Length(max=500, message="API密钥不能超过500个字符"),
        ],
    )
    is_active = BooleanField("is_active", default=True)
    remark = StringField(
        "remark", validators=[Length(max=255, message="备注不能超过255个字符")]
    )


class UpdateProviderApiKeyReq(FlaskForm):
    """更新提供商API密钥请求"""

    api_key = StringField(
        "api_key", validators=[Length(max=500, message="API密钥不能超过500个字符")]
    )
    is_active = BooleanField("is_active")
    remark = StringField(
        "remark", validators=[Length(max=255, message="备注不能超过255个字符")]
    )


class GetProviderApiKeysResp(Schema):
    """获取提供商API密钥列表响应"""

    id = fields.UUID(dump_default="")
    provider_name = fields.String(dump_default="")
    api_key_masked = fields.String(dump_default="")  # 脱敏后的密钥
    is_active = fields.Boolean(dump_default=True)
    remark = fields.String(dump_default="")
    updated_at = fields.Integer(dump_default=0)
    created_at = fields.Integer(dump_default=0)

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
            "is_active": data.is_active,
            "remark": data.remark,
            "updated_at": datetime_to_timestamp(data.updated_at),
            "created_at": datetime_to_timestamp(data.created_at),
        }
