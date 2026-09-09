from flask_wtf import FlaskForm
from marshmallow import Schema, fields, pre_dump
from wtforms import BooleanField, IntegerField, StringField, ValidationError
from wtforms.validators import DataRequired, Length, NumberRange, Optional, URL

from internal.core.mcp.entities import MCPTransport
from internal.lib.helper import datetime_to_timestamp
from internal.model import MCPServer, MCPTool
from pkg.paginator import PaginatorReq
from .schema import DictField, ListField

_SECRET_MASK = "********"


def _mask_env(env):
    return {str(key): _SECRET_MASK for key in (env or {})}


def _mask_headers(headers):
    return [
        {"key": str(item.get("key", "")), "value": _SECRET_MASK}
        for item in (headers or [])
        if isinstance(item, dict)
    ]


def _build_tool_inputs(input_schema: dict) -> list[dict]:
    properties = input_schema.get("properties", {}) if isinstance(input_schema, dict) else {}
    required_fields = set(input_schema.get("required", [])) if isinstance(input_schema, dict) else set()
    inputs = []
    for field_name, field_schema in properties.items():
        inputs.append(
            {
                "name": field_name,
                "type": field_schema.get("type", "string"),
                "required": field_name in required_fields,
                "description": field_schema.get("description", ""),
            }
        )
    return inputs


class DebugMCPToolReq(FlaskForm):
    parameters = DictField("parameters", default={})


class GetMCPServersWithPageReq(PaginatorReq):
    search_word = StringField("search_word", validators=[Optional()])


class CreateMCPServerReq(FlaskForm):
    name = StringField(
        "name",
        validators=[
            DataRequired(message="MCP 服务名称不能为空"),
            Length(min=1, max=60, message="MCP 服务名称长度范围为 1-60"),
        ],
    )
    icon = StringField(
        "icon",
        validators=[
            Optional(),
            URL(message="icon must be a valid URL"),
        ],
    )
    description = StringField(
        "description",
        validators=[Optional(), Length(max=800, message="MCP 服务描述最长 800 字")],
    )
    transport = StringField("transport", validators=[DataRequired(message="transport 不能为空")])
    enabled = BooleanField("enabled", default=True)
    url = StringField("url", validators=[Optional(), URL(message="url 必须是合法链接")])
    command = StringField("command", validators=[Optional(), Length(max=255)])
    args = ListField("args", default=[])
    env = DictField("env", default={})
    headers = ListField("headers", default=[])
    timeout = IntegerField(
        "timeout",
        default=30,
        validators=[Optional(), NumberRange(min=1, max=300, message="timeout 范围为 1-300")],
    )

    def validate_transport(self, field: StringField):
        if field.data not in [MCPTransport.STDIO.value, MCPTransport.HTTP.value]:
            raise ValidationError("仅支持 stdio/http 两种 transport")

    def validate_args(self, field: ListField):
        if not isinstance(field.data, list):
            raise ValidationError("args 必须是字符串数组")
        for item in field.data:
            if not isinstance(item, str):
                raise ValidationError("args 必须是字符串数组")

    def validate_env(self, field: DictField):
        if field.data is None:
            field.data = {}
            return
        if not isinstance(field.data, dict):
            raise ValidationError("env 必须是对象")
        for key, value in field.data.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValidationError("env 的 key/value 必须都是字符串")

    def validate_headers(self, field: ListField):
        if not isinstance(field.data, list):
            raise ValidationError("headers 必须是数组")
        for header in field.data:
            if not isinstance(header, dict) or set(header.keys()) != {"key", "value"}:
                raise ValidationError("headers 中每项都必须包含 key/value")
            if not isinstance(header["key"], str) or not isinstance(header["value"], str):
                raise ValidationError("headers 中 key/value 必须都是字符串")

    def validate(self, extra_validators=None):
        if not super().validate(extra_validators=extra_validators):
            return False

        if self.transport.data == MCPTransport.STDIO.value:
            if not self.command.data:
                self.command.errors = ["stdio 模式下 command 不能为空"]
                return False
        elif self.transport.data == MCPTransport.HTTP.value:
            if not self.url.data:
                self.url.errors = ["http 模式下 url 不能为空"]
                return False

        return True


class UpdateMCPServerReq(CreateMCPServerReq):
    pass


class GetMCPServerResp(Schema):
    id = fields.UUID()
    name = fields.String()
    icon = fields.String()
    description = fields.String()
    transport = fields.String()
    enabled = fields.Boolean()
    url = fields.String()
    command = fields.String()
    args = fields.List(fields.String(), dump_default=[])
    env = fields.Dict(dump_default={})
    headers = fields.List(fields.Dict(), dump_default=[])
    timeout = fields.Integer(dump_default=30)
    tools = fields.List(fields.Dict(), dump_default=[])
    updated_at = fields.Integer(dump_default=0)
    created_at = fields.Integer(dump_default=0)

    @pre_dump
    def process_data(self, data: MCPServer, **kwargs):
        return {
            "id": data.id,
            "name": data.name,
            "icon": data.icon,
            "description": data.description,
            "transport": data.transport,
            "enabled": data.enabled,
            "url": data.url,
            "command": data.command,
            "args": data.args,
            "env": _mask_env(data.env),
            "headers": _mask_headers(data.headers),
            "timeout": int(data.timeout or 30),
            "tools": [
                {
                    "id": tool.name,
                    "name": tool.name,
                    "description": tool.description,
                    "inputs": _build_tool_inputs(tool.input_schema),
                }
                for tool in data.tools
            ],
            "updated_at": datetime_to_timestamp(data.updated_at),
            "created_at": datetime_to_timestamp(data.created_at),
        }


class GetMCPToolResp(Schema):
    id = fields.UUID()
    name = fields.String()
    description = fields.String()
    inputs = fields.List(fields.Dict(), dump_default=[])
    provider = fields.Dict()

    @pre_dump
    def process_data(self, data: MCPTool, **kwargs):
        provider = data.server
        return {
            "id": data.id,
            "name": data.name,
            "description": data.description,
            "inputs": _build_tool_inputs(data.input_schema),
            "provider": {
                "id": provider.id,
                "name": provider.name,
                "icon": provider.icon,
                "description": provider.description,
                "transport": provider.transport,
            },
        }


class GetMCPServersWithPageResp(Schema):
    id = fields.UUID()
    name = fields.String()
    icon = fields.String()
    description = fields.String()
    transport = fields.String()
    enabled = fields.Boolean()
    tools = fields.List(fields.Dict(), dump_default=[])
    updated_at = fields.Integer(dump_default=0)
    created_at = fields.Integer(dump_default=0)

    @pre_dump
    def process_data(self, data: MCPServer, **kwargs):
        return {
            "id": data.id,
            "name": data.name,
            "icon": data.icon,
            "description": data.description,
            "transport": data.transport,
            "enabled": data.enabled,
            "tools": [
                {
                    "id": tool.name,
                    "name": tool.name,
                    "description": tool.description,
                    "inputs": _build_tool_inputs(tool.input_schema),
                }
                for tool in data.tools
            ],
            "updated_at": datetime_to_timestamp(data.updated_at),
            "created_at": datetime_to_timestamp(data.created_at),
        }


class DebugMCPToolResp(Schema):
    success = fields.Boolean()
    result = fields.Raw(allow_none=True)
    error = fields.String(allow_none=True)
