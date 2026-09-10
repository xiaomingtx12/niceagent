"""内置插件目录响应，配置状态不包含凭据值。"""

from marshmallow import Schema, fields


class BuiltinToolDefinitionResp(Schema):
    """工具参数与模型输入分别序列化，兼容现有编排页面。"""

    name = fields.String()
    label = fields.String()
    description = fields.String()
    params = fields.List(fields.Dict())
    inputs = fields.List(fields.Dict())


class BuiltinProviderResp(Schema):
    """保留旧 available 字段兼容读取，实际配置状态独立表达。"""

    name = fields.String()
    label = fields.String()
    description = fields.String()
    background = fields.String()
    category = fields.String()
    created_at = fields.Integer()
    available = fields.Boolean()
    required_env = fields.List(fields.String())
    network_required = fields.Boolean()
    setup_hint = fields.String()
    requires_app_configuration = fields.Boolean()
    configuration_status = fields.String()
    missing_config = fields.List(fields.String())
    tools = fields.List(fields.Nested(BuiltinToolDefinitionResp))


class GetBuiltinToolResp(BuiltinToolDefinitionResp):
    """工具详情附带提供商与部署配置摘要。"""

    provider = fields.Nested(BuiltinProviderResp)
    created_at = fields.Integer()
    configuration_status = fields.String()
    missing_config = fields.List(fields.String())
