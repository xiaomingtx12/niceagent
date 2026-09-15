"""Agent 执行历史事件查询。"""

from dataclasses import dataclass
from uuid import UUID

from flask import Response, request
from flask_login import current_user, login_required
from injector import inject

from internal.exception import ValidateErrorException
from internal.service.agent.agent_execution_query import AgentExecutionQuery
from pkg.paginator import PaginatorReq
from pkg.response import json_response


@inject
@dataclass
class AgentExecutionHandler:
    """仅按执行标识查询，归属校验由查询服务完成。"""

    query_service: AgentExecutionQuery

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
