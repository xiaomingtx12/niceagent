"""将客户、项目和工单读写限定在编排者配置的飞书多维表格。"""

import json
from typing import ClassVar

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from internal.core.tools.builtin_tools.business_http import (
    BusinessToolError,
    api_request,
    json_object,
    resource_id,
)
from internal.lib.helper import add_attribute


class FieldsArgs(BaseModel):
    """分页读取字段定义，为记录写入提供真实字段类型。"""

    page_token: str = Field(
        default="",
        max_length=2048,
        description="续页时传入上次返回的 page_token；首次留空。",
    )


class SearchArgs(FieldsArgs):
    """筛选条件使用飞书原生 JSON，兼容工作流文本变量。"""

    filter_json: str = Field(
        default="{}",
        max_length=12000,
        description='飞书筛选 JSON，如 {"conjunction":"and","conditions":[{"field_name":"状态","operator":"is","value":["跟进中"]}]}；空对象查询全部。',
    )
    page_size: int = Field(
        default=20, ge=1, le=100, description="单页记录数，最多 100。"
    )


class CreateArgs(BaseModel):
    """写入字段必须匹配已查询的表结构。"""

    fields_json: str = Field(
        min_length=2,
        max_length=50000,
        description='字段名到值的 JSON 对象，例如 {"客户名称":"示例公司","状态":"跟进中"}。日期使用毫秒时间戳；关联和人员字段先查询字段结构。',
    )


class UpdateArgs(CreateArgs):
    """更新只影响指定记录中明确提交的字段。"""

    record_id: str = Field(
        min_length=1,
        max_length=128,
        description="从记录查询结果取得的 record_id，禁止猜测。",
    )


class FeishuTool(BaseTool):
    """凭据与目标表只接受应用配置，模型不能选择其他目标表。"""

    app_id: str = Field(default="", exclude=True, repr=False)
    app_secret: str = Field(default="", exclude=True, repr=False)
    app_token: str = Field(default="", exclude=True, repr=False)
    table_id: str = Field(default="", exclude=True, repr=False)
    operation: ClassVar[str]

    def _run(self, **kwargs) -> str:
        """先校验输入，再交换租户令牌；失败返回脱敏错误且不自动重试。"""
        try:
            if not self.app_id.strip() or not self.app_secret.strip():
                raise BusinessToolError(
                    "请在工具配置中填写飞书自建应用 App ID 和 App Secret。"
                )
            base = f"bitable/v1/apps/{resource_id(self.app_token, '多维表格 app_token')}/tables/{resource_id(self.table_id, 'table_id')}"
            payload, params = None, None
            if self.operation == "fields":
                method, path = "GET", base + "/fields"
                params = {"page_size": 100}
                if kwargs.get("page_token"):
                    params["page_token"] = kwargs["page_token"]
            elif self.operation == "search":
                method, path = "POST", base + "/records/search"
                filters = json_object(kwargs.get("filter_json", "{}"), "筛选条件")
                payload = {"filter": filters} if filters else {}
                params = {"page_size": kwargs.get("page_size", 20)}
                if kwargs.get("page_token"):
                    params["page_token"] = kwargs["page_token"]
            else:
                values = json_object(kwargs["fields_json"], "字段数据")
                if not values:
                    raise BusinessToolError("至少填写一个需要写入的字段。")
                payload = {"fields": values}
                method, path = "POST", base + "/records"
                if self.operation == "update":
                    method, path = "PUT", path + "/" + resource_id(
                        kwargs["record_id"], "record_id"
                    )
            auth = api_request(
                "feishu",
                "POST",
                "auth/v3/tenant_access_token/internal",
                payload={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            token = auth.get("tenant_access_token")
            if not isinstance(token, str) or not token:
                raise BusinessToolError(
                    "飞书没有返回有效租户令牌，请检查自建应用配置。"
                )
            result = api_request(
                "feishu", method, path, token=token, payload=payload, params=params
            )
            return json.dumps(
                {"operation": self.operation, "data": result.get("data", {})},
                ensure_ascii=False,
            )
        except BusinessToolError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)


class FeishuFieldsTool(FeishuTool):
    """读取字段名、类型与选项，供查询和写入前核对。"""

    name: str = "feishu_table_fields"
    description: str = (
        "读取已配置飞书多维表格的字段结构与选项。创建或更新客户、工单、项目记录前先调用此工具核查字段类型。"
    )
    args_schema: type[BaseModel] = FieldsArgs
    operation: ClassVar[str] = "fields"


class FeishuSearchTool(FeishuTool):
    """查询业务记录并返回后续分页游标。"""

    name: str = "feishu_record_search"
    description: str = (
        "筛选查询已配置飞书多维表格中的客户、销售线索、项目或工单，返回记录 ID、字段和分页信息。"
    )
    args_schema: type[BaseModel] = SearchArgs
    operation: ClassVar[str] = "search"


class FeishuCreateTool(FeishuTool):
    """创建业务记录，避免隐藏的覆盖或自动重试。"""

    name: str = "feishu_record_create"
    description: str = (
        "在已配置飞书多维表格中创建一条客户、项目或工单记录。这会实际写入；仅在用户要求创建时使用，先查询避免重复。"
    )
    args_schema: type[BaseModel] = CreateArgs
    operation: ClassVar[str] = "create"


class FeishuUpdateTool(FeishuTool):
    """仅修改已查询到的记录与明确指定的字段。"""

    name: str = "feishu_record_update"
    description: str = (
        "更新飞书多维表格指定记录的部分字段，例如销售阶段、跟进摘要或工单状态。这会实际写入；先查询确认记录和用户要求修改的字段。"
    )
    args_schema: type[BaseModel] = UpdateArgs
    operation: ClassVar[str] = "update"


@add_attribute("args_schema", FieldsArgs)
def feishu_table_fields(**kwargs) -> BaseTool:
    """从编排配置构建只读字段工具。"""
    return FeishuFieldsTool(**kwargs)


@add_attribute("args_schema", SearchArgs)
def feishu_record_search(**kwargs) -> BaseTool:
    """从编排配置构建只读查询工具。"""
    return FeishuSearchTool(**kwargs)


@add_attribute("args_schema", CreateArgs)
def feishu_record_create(**kwargs) -> BaseTool:
    """仅显式绑定时授予记录创建能力。"""
    return FeishuCreateTool(**kwargs)


@add_attribute("args_schema", UpdateArgs)
def feishu_record_update(**kwargs) -> BaseTool:
    """仅显式绑定时授予记录更新能力。"""
    return FeishuUpdateTool(**kwargs)
