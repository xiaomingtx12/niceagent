"""Agent Run 历史事件查询。"""

from dataclasses import dataclass
from uuid import UUID

from flask import Response, request
from flask_login import current_user, login_required
from injector import inject

from internal.exception import ValidateErrorException
from internal.service.agent.agent_run_query import AgentRunQuery
from pkg.paginator import PaginatorReq
from pkg.response import json_response


@inject
@dataclass
class AgentRunHandler:
    """仅按 Run 标识查询，归属校验由查询服务完成。"""

    query_service: AgentRunQuery

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
