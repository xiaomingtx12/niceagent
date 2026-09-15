"""Agent 执行历史事件查询。"""

from dataclasses import dataclass
from uuid import UUID

from flask import Response, request
from flask_login import current_user, login_required
from injector import inject
from pydantic import ValidationError

from internal.exception import ValidateErrorException
from internal.service.agent.agent_execution_query import AgentExecutionQuery
from internal.schema.agent.agent_protocol_schema import AgentProtocolRunStatusesReq
from pkg.paginator import PaginatorReq
from pkg.response import json_response


@inject
@dataclass
class AgentExecutionHandler:
    """仅按执行标识查询，归属校验由查询服务完成。"""

    query_service: AgentExecutionQuery

    @login_required
    def statuses(self) -> Response:
        """重复 run_id 查询参数由 Schema 限量并校验，服务端逐 Run 过滤权限。"""
        try:
            payload = AgentProtocolRunStatusesReq.model_validate({"run_ids": request.args.getlist("run_id")})
        except ValidationError as exc:
            raise ValidateErrorException("请提供 1 至 50 个有效运行 ID") from exc
        result = self.query_service.get_run_statuses(run_ids=payload.run_ids, account=current_user)
        return json_response(result.model_dump(mode="json"))

    @login_required
    def result(self, run_id: UUID) -> Response:
        """结果与过程分开读取，打开回答不触发历史事件扫描。"""
        result = self.query_service.get_run_result(run_id=run_id, account=current_user)
        return json_response(result.model_dump(mode="json"))

    @login_required
    def snapshot(self, run_id: UUID) -> Response:
        """按已有会话所有权返回合并后的完整执行过程。"""
        result = self.query_service.get_recovery_snapshot(run_id=run_id, account=current_user)
        return json_response(result.model_dump(mode="json"))

    @login_required
    def events(self, run_id: UUID) -> Response:
        """分页参数在入口校验，运行详情只能由所属账号读取。"""
        page = PaginatorReq(request.args)
        if not page.validate():
            raise ValidateErrorException("执行详情分页参数无效")
        result = self.query_service.list_run_events(
            run_id=run_id, account=current_user,
            current_page=page.current_page.data, page_size=page.page_size.data,
        )
        return json_response(result.model_dump(mode="json"))
