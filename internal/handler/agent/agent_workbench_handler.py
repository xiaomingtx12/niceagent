"""Agent 工作台处理器。"""

from dataclasses import dataclass
from uuid import UUID

from flask import Response, request
from flask_login import current_user, login_required
from injector import inject
from internal.exception import ValidateErrorException
from internal.schema.agent.agent_workbench_read_schema import WorkbenchStateReq
from pkg.paginator import PaginatorReq

from internal.schema import (
    WorkbenchCancelResp,
    WorkbenchCommandReq,
    WorkbenchRunObservabilityResp,
    WorkbenchStateResp,
    WorkbenchEventStreamReq,
)
from internal.service import (
    AgentWorkbenchService,
    AppService,
    CreatorTaskService,
    WebAppService,
)
from internal.service.agent_protocol.command_adapter import execute_command
from internal.service.agent.agent_workbench_query_service import AgentWorkbenchQueryService
from pkg.response import json_response, sse_response


@inject
@dataclass
class AgentWorkbenchHandler:
    """Agent 工作台 HTTP 处理器。"""

    query_service: AgentWorkbenchQueryService
    service: AgentWorkbenchService
    app_service: AppService
    web_app_service: WebAppService
    creator_task_service: CreatorTaskService

    @login_required
    def debug_model_settings(self, app_id: UUID, conversation_id: UUID) -> Response:
        """沿用调试会话权限，仅返回模型选项。"""
        result = self.query_service.get_model_settings(context=self.app_service.get_debugger_invocation_context(
            app_id=app_id, conversation_id=conversation_id, account=current_user,
        ))
        return json_response(result.model_dump(mode="json"))

    @login_required
    def web_app_model_settings(self, token: str, conversation_id: UUID) -> Response:
        """发布态选项沿用会话授权，不暴露创建者可选模型目录。"""
        result = self.query_service.get_model_settings(context=self.web_app_service.get_invocation_context(
            token=token, conversation_id=conversation_id, account=current_user,
        ))
        return json_response(result.model_dump(mode="json"))

    @login_required
    def creator_task_model_settings(self, app_id: UUID, conversation_id: UUID) -> Response:
        """任务中心独立读取当前会话的模型选择。"""
        result = self.query_service.get_model_settings(context=self.creator_task_service.get_invocation_context(
            app_id=app_id, conversation_id=conversation_id, account=current_user,
        ))
        return json_response(result.model_dump(mode="json"))

    @login_required
    def run_events(self, run_id: UUID) -> Response:
        """分页参数在 HTTP 边界校验，Run 所有权交由服务复核。"""
        page = PaginatorReq(request.args)
        if not page.validate():
            raise ValidateErrorException("执行详情分页参数无效")
        result = self.query_service.get_run_events(
            run_id=run_id, account=current_user,
            current_page=page.current_page.data, page_size=page.page_size.data,
        )
        return json_response(result.model_dump(mode="json"))

    @login_required
    def run_observability(self, run_id: UUID) -> Response:
        """读取当前账号可访问的 Agent Run 平台观测链路。"""
        result = self.query_service.get_run_observability(
            run_id=run_id,
            account=current_user,
        )
        response = WorkbenchRunObservabilityResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def debug_command(self, app_id: UUID, conversation_id: UUID) -> Response:
        """调试工作台提交 Agent 命令。"""
        body = request.get_json(silent=True)
        command = WorkbenchCommandReq.from_mapping(body)
        result = execute_command(
            self.service,
            context=self.app_service.get_debugger_invocation_context(
                app_id=app_id,
                conversation_id=conversation_id,
                account=current_user,
            ),
            command=command,
        )
        return json_response(result.model_dump(mode="json", exclude_none=True))

    @login_required
    def debug_stream(self, app_id: UUID, conversation_id: UUID) -> Response:
        """订阅调试工作台的 Agent 事件。"""
        stream_request = WorkbenchEventStreamReq.from_mapping(
            request.get_json(silent=True),
            last_event_id=request.headers.get("Last-Event-ID"),
        )
        events = self.query_service.subscribe(
            context=self.app_service.get_debugger_invocation_context(
                app_id=app_id,
                conversation_id=conversation_id,
                account=current_user,
            ),
            stream_request=stream_request,
        )
        return sse_response(events)

    @login_required
    def debug_state(self, app_id: UUID, conversation_id: UUID) -> Response:
        """读取调试工作台状态和消息快照。"""
        result = self.query_service.get_state(
            view=WorkbenchStateReq.from_query(request.args).view.data,
            context=self.app_service.get_debugger_invocation_context(
                app_id=app_id,
                conversation_id=conversation_id,
                account=current_user,
            ),
        )
        response = WorkbenchStateResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def web_app_command(self, token: str, conversation_id: UUID) -> Response:
        """发布态 WebApp 提交 Agent 命令。"""
        body = request.get_json(silent=True)
        command = WorkbenchCommandReq.from_mapping(body)
        result = execute_command(
            self.service,
            context=self.web_app_service.get_invocation_context(
                token=token,
                conversation_id=conversation_id,
                account=current_user,
            ),
            command=command,
        )
        return json_response(result.model_dump(mode="json", exclude_none=True))

    @login_required
    def web_app_stream(self, token: str, conversation_id: UUID) -> Response:
        """订阅发布态 WebApp 的 Agent 事件。"""
        stream_request = WorkbenchEventStreamReq.from_mapping(
            request.get_json(silent=True),
            last_event_id=request.headers.get("Last-Event-ID"),
        )
        events = self.query_service.subscribe(
            context=self.web_app_service.get_invocation_context(
                token=token,
                conversation_id=conversation_id,
                account=current_user,
            ),
            stream_request=stream_request,
        )
        return sse_response(events)

    @login_required
    def web_app_state(self, token: str, conversation_id: UUID) -> Response:
        """读取发布态 WebApp 工作台状态和消息快照。"""
        result = self.query_service.get_state(
            view=WorkbenchStateReq.from_query(request.args).view.data,
            context=self.web_app_service.get_invocation_context(
                token=token,
                conversation_id=conversation_id,
                account=current_user,
            ),
        )
        response = WorkbenchStateResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def debug_cancel_run(
        self,
        app_id: UUID,
        conversation_id: UUID,
        run_id: UUID,
    ) -> Response:
        """取消调试工作台的指定 Agent Run。"""
        result = self.service.cancel_run(
            context=self.app_service.get_debugger_invocation_context(
                app_id=app_id,
                conversation_id=conversation_id,
                account=current_user,
            ),
            run_id=run_id,
        )
        response = WorkbenchCancelResp.model_validate(result, from_attributes=True)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def web_app_cancel_run(
        self,
        token: str,
        conversation_id: UUID,
        run_id: UUID,
    ) -> Response:
        """取消发布态 WebApp 的指定 Agent Run。"""
        result = self.service.cancel_run(
            context=self.web_app_service.get_invocation_context(
                token=token,
                conversation_id=conversation_id,
                account=current_user,
            ),
            run_id=run_id,
        )
        response = WorkbenchCancelResp.model_validate(result, from_attributes=True)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def creator_task_command(self, app_id: UUID, conversation_id: UUID) -> Response:
        """Creator 任务中心提交已发布 Agent 命令。"""
        body = request.get_json(silent=True)
        command = WorkbenchCommandReq.from_mapping(body)
        result = execute_command(
            self.service,
            context=self.creator_task_service.get_invocation_context(
                app_id=app_id,
                conversation_id=conversation_id,
                account=current_user,
            ),
            command=command,
        )
        return json_response(result.model_dump(mode="json", exclude_none=True))

    @login_required
    def creator_task_stream(self, app_id: UUID, conversation_id: UUID) -> Response:
        """Creator 任务中心订阅 Agent 事件。"""
        stream_request = WorkbenchEventStreamReq.from_mapping(
            request.get_json(silent=True),
            last_event_id=request.headers.get("Last-Event-ID"),
        )
        events = self.query_service.subscribe(
            context=self.creator_task_service.get_invocation_context(
                app_id=app_id,
                conversation_id=conversation_id,
                account=current_user,
            ),
            stream_request=stream_request,
        )
        return sse_response(events)

    @login_required
    def creator_task_state(self, app_id: UUID, conversation_id: UUID) -> Response:
        """Creator 任务中心读取会话状态。"""
        result = self.query_service.get_state(
            view=WorkbenchStateReq.from_query(request.args).view.data,
            context=self.creator_task_service.get_invocation_context(
                app_id=app_id,
                conversation_id=conversation_id,
                account=current_user,
            ),
        )
        response = WorkbenchStateResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def creator_task_cancel_run(
        self, app_id: UUID, conversation_id: UUID, run_id: UUID
    ) -> Response:
        """Creator 任务中心取消指定 Agent Run。"""
        result = self.service.cancel_run(
            context=self.creator_task_service.get_invocation_context(
                app_id=app_id,
                conversation_id=conversation_id,
                account=current_user,
            ),
            run_id=run_id,
        )
        response = WorkbenchCancelResp.model_validate(result, from_attributes=True)
        return json_response(response.model_dump(mode="json"))


__all__ = ["AgentWorkbenchHandler"]
