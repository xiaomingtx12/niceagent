from flask_wtf import FlaskForm
from marshmallow import Schema, fields, pre_dump
from wtforms import StringField
from wtforms.validators import DataRequired, UUID, Length

from internal.core.builtin_workflows.entities.builtin_workflow_entity import (
    BuiltinWorkflowEntity,
)
from internal.core.builtin_workflows.entities.category_entity import (
    WorkflowCategoryEntity,
)


class GetBuiltinWorkflowCategoriesResp(Schema):
    """获取内置工作流模板分类列表响应"""

    category = fields.String(dump_default="")
    name = fields.String(dump_default="")

    @pre_dump
    def process_data(self, data: WorkflowCategoryEntity, **kwargs):
        return data.model_dump()


class GetBuiltinWorkflowsResp(Schema):
    """获取内置工作流模板列表响应"""

    id = fields.String(dump_default="")
    category = fields.String(dump_default="")
    name = fields.String(dump_default="")
    tool_call_name = fields.String(dump_default="")
    icon = fields.String(dump_default="")
    description = fields.String(dump_default="")
    created_at = fields.Integer(dump_default=0)

    @pre_dump
    def process_data(self, data: BuiltinWorkflowEntity, **kwargs):
        return data.model_dump(
            include={
                "id",
                "category",
                "name",
                "tool_call_name",
                "icon",
                "description",
                "created_at",
            }
        )


class GetBuiltinWorkflowDetailResp(Schema):
    """获取内置工作流模板详情响应"""

    id = fields.String(dump_default="")
    category = fields.String(dump_default="")
    name = fields.String(dump_default="")
    tool_call_name = fields.String(dump_default="")
    icon = fields.String(dump_default="")
    description = fields.String(dump_default="")
    nodes = fields.List(fields.Dict(), dump_default=[])
    edges = fields.List(fields.Dict(), dump_default=[])
    created_at = fields.Integer(dump_default=0)

    @pre_dump
    def process_data(self, data: BuiltinWorkflowEntity, **kwargs):
        return data.model_dump()


class CreateWorkflowFromTemplateReq(FlaskForm):
    """从模板创建工作流请求"""

    name = StringField(
        "name",
        default="",
        validators=[
            DataRequired("工作流名称不能为空"),
            Length(min=1, max=255, message="工作流名称长度必须在1-255之间"),
        ],
    )
