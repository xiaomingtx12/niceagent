"""测评 HTTP 边界；普通聊天协议无需感知测评能力。"""

from dataclasses import dataclass
from uuid import UUID

from flask import request
from flask_login import current_user, login_required
from injector import inject
from pydantic import ValidationError

from internal.middleware.rate_limit_middleware import rate_limit
from internal.schema.agent_evaluation.agent_evaluation_schema import (
    AgentEvaluationReq,
    AgentEvaluationResp,
    EvaluationPageResp,
    EvaluationSourcePageResp,
    EvaluationSourceResp,
)
from internal.service.agent_evaluation.agent_evaluation_service import AgentEvaluationService
from pkg.paginator import PaginatorReq
from pkg.response import success_json, validate_error_json


@inject
@dataclass
class AgentEvaluationHandler:
    """身份由登录态提供，客户端不能指定评分记录所属账号。"""

    service: AgentEvaluationService

    @login_required
    def evaluation_sources(self, app_id: UUID):
        """统一解析页码，服务层不读取 HTTP 查询串。"""
        req = PaginatorReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        result = self.service.list_sources(
            app_id, current_user, req.current_page.data, req.page_size.data
        )
        return success_json(EvaluationSourcePageResp().dump(result))

    @login_required
    def evaluation_source(self, app_id: UUID, run_id: UUID):
        """显示将实际送交评分的完整文本。"""
        return success_json(
            EvaluationSourceResp().dump(
                self.service.get_source(app_id, run_id, current_user)
            )
        )

    @login_required
    @rate_limit(config_key="chat")
    def create_evaluation(self, app_id: UUID):
        """结构化请求不允许注入答案、凭证或模型端点。"""
        try:
            req = AgentEvaluationReq.model_validate(request.get_json(silent=True))
        except ValidationError:
            return validate_error_json(
                {
                    "evaluation": [
                        "请选择历史任务、评分模型，并填写 1–12 条不重复标准，每条最多 500 字"
                    ]
                }
            )
        return success_json(
            AgentEvaluationResp().dump(self.service.submit(app_id, current_user, req))
        )

    @login_required
    def evaluation_history(self, app_id: UUID):
        """历史列表省略原始长文本，详情接口再按需读取。"""
        req = PaginatorReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        result = self.service.list_evaluations(
            app_id, current_user, req.current_page.data, req.page_size.data
        )
        return success_json(
            EvaluationPageResp(exclude=("list.source_snapshot.answer",)).dump(result)
        )

    @login_required
    def evaluation_detail(self, app_id: UUID, evaluation_id: UUID):
        """返回评分当时的快照和逐项结论。"""
        return success_json(
            AgentEvaluationResp().dump(
                self.service.get_evaluation(app_id, evaluation_id, current_user)
            )
        )
