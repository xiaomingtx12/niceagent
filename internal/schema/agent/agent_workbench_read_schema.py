"""工作台读取模式与分页执行详情 DTO。"""

from uuid import UUID
from typing import Literal

from flask_wtf import FlaskForm
from pydantic import BaseModel
from wtforms import SelectField
from werkzeug.datastructures import MultiDict

from internal.exception import ValidateErrorException
from internal.schema.agent.agent_workbench_schema import WorkbenchEvent

WorkbenchStateView = Literal["full", "compact", "status"]


class WorkbenchStateReq(FlaskForm):
    """完整模式保留原协议；精简模式供支持按需详情的客户端使用。"""

    view = SelectField(choices=["full", "compact", "status"], default="full")

    @classmethod
    def from_query(cls, args: MultiDict[str, str]) -> "WorkbenchStateReq":
        """在 HTTP 边界校验模式，不把原始查询参数传入服务。"""
        form = cls(args)
        if not form.validate():
            raise ValidateErrorException("state view 必须为 full、compact 或 status")
        return form


class WorkbenchRunEventsResp(BaseModel):
    """单个 Run 的执行详情按稳定序号升序分页。"""

    run_id: UUID
    events: list[WorkbenchEvent]
    next_page: int | None = None


class WorkbenchEventBatchResp(BaseModel):
    """扫描游标独立于公开事件，过滤结果为空时也能继续读取下一批。"""

    events: list[WorkbenchEvent]
    next_sequence: int
    has_more: bool
