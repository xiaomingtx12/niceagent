"""Agent 工作台处理器。"""

from dataclasses import dataclass
from uuid import UUID

from flask import Response, request
from flask_login import current_user, login_required
from injector import inject

from internal.entity.conversation_entity import InvokeFrom
from internal.schema import (
    WorkbenchCancelResp,
    WorkbenchCommandReq,
    WorkbenchMessagePageResp,
    WorkbenchRunObservabilityResp,
    WorkbenchStateResp,
    WorkbenchSubscriptionReq,
)
from pkg.paginator import CursorPaginatorReq
from internal.service import AgentWorkbenchService
from pkg.response import json_response, sse_response, validate_error_json


@inject
@dataclass
class AgentWorkbenchHandler:
    """Agent 工作台 HTTP 处理器。"""

    service: AgentWorkbenchService

    @login_required
    def run_observability(self, run_id: UUID) -> Response:
        """读取当前账号可访问的 Agent Run 平台观测链路。"""
        result = self.service.get_run_observability(
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
        result = self.service.start_run(
            invoke_from=InvokeFrom.DEBUGGER,
            access_value=str(app_id),
            conversation_id=conversation_id,
            account=current_user,
            run_input=command.to_run_input(),
            command_id=command.command_id,
        )
        return json_response(result.model_dump(mode="json", exclude_none=True))

    @login_required
    def debug_stream(self, app_id: UUID, conversation_id: UUID) -> Response:
        """订阅调试工作台的 Agent 事件。"""
        subscription = WorkbenchSubscriptionReq.from_mapping(
            request.get_json(silent=True),
            last_event_id=request.headers.get("Last-Event-ID"),
        )
        events = self.service.subscribe(
            invoke_from=InvokeFrom.DEBUGGER,
            access_value=str(app_id),
            conversation_id=conversation_id,
            account=current_user,
            subscription=subscription,
        )
        return sse_response(events)

    @login_required
    def debug_state(self, app_id: UUID, conversation_id: UUID) -> Response:
        """读取调试工作台状态和消息快照。"""
        result = self.service.get_state(
            invoke_from=InvokeFrom.DEBUGGER,
            access_value=str(app_id),
            conversation_id=conversation_id,
            account=current_user,
        )
        response = WorkbenchStateResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def debug_messages(self, app_id: UUID, conversation_id: UUID) -> Response:
        """读取调试工作台历史消息分页。"""
        query = CursorPaginatorReq(request.args)
        if not query.validate():
            return validate_error_json(query.errors)
        result = self.service.get_message_page(
            invoke_from=InvokeFrom.DEBUGGER,
            access_value=str(app_id),
            conversation_id=conversation_id,
            account=current_user,
            before=query.before_uuid,
            page_size=query.page_size.data,
        )
        response = WorkbenchMessagePageResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def web_app_command(self, token: str, conversation_id: UUID) -> Response:
        """发布态 WebApp 提交 Agent 命令。"""
        body = request.get_json(silent=True)
        command = WorkbenchCommandReq.from_mapping(body)
        result = self.service.start_run(
            invoke_from=InvokeFrom.WEB_APP,
            access_value=token,
            conversation_id=conversation_id,
            account=current_user,
            run_input=command.to_run_input(),
            command_id=command.command_id,
        )
        return json_response(result.model_dump(mode="json", exclude_none=True))

    @login_required
    def web_app_stream(self, token: str, conversation_id: UUID) -> Response:
        """订阅发布态 WebApp 的 Agent 事件。"""
        subscription = WorkbenchSubscriptionReq.from_mapping(
            request.get_json(silent=True),
            last_event_id=request.headers.get("Last-Event-ID"),
        )
        events = self.service.subscribe(
            invoke_from=InvokeFrom.WEB_APP,
            access_value=token,
            conversation_id=conversation_id,
            account=current_user,
            subscription=subscription,
        )
        return sse_response(events)

    @login_required
    def web_app_state(self, token: str, conversation_id: UUID) -> Response:
        """读取发布态 WebApp 工作台状态和消息快照。"""
        result = self.service.get_state(
            invoke_from=InvokeFrom.WEB_APP,
            access_value=token,
            conversation_id=conversation_id,
            account=current_user,
        )
        response = WorkbenchStateResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def web_app_messages(self, token: str, conversation_id: UUID) -> Response:
        """读取发布态 WebApp 历史消息分页。"""
        query = CursorPaginatorReq(request.args)
        if not query.validate():
            return validate_error_json(query.errors)
        result = self.service.get_message_page(
            invoke_from=InvokeFrom.WEB_APP,
            access_value=token,
            conversation_id=conversation_id,
            account=current_user,
            before=query.before_uuid,
            page_size=query.page_size.data,
        )
        response = WorkbenchMessagePageResp.model_validate(result)
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
            invoke_from=InvokeFrom.DEBUGGER,
            access_value=str(app_id),
            conversation_id=conversation_id,
            run_id=run_id,
            account=current_user,
        )
        response = WorkbenchCancelResp.model_validate(result)
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
            invoke_from=InvokeFrom.WEB_APP,
            access_value=token,
            conversation_id=conversation_id,
            run_id=run_id,
            account=current_user,
        )
        response = WorkbenchCancelResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def creator_task_command(self, app_id: UUID, conversation_id: UUID) -> Response:
        """Creator 任务中心提交已发布 Agent 命令。"""
        body = request.get_json(silent=True)
        command = WorkbenchCommandReq.from_mapping(body)
        result = self.service.start_run(
            invoke_from=InvokeFrom.CREATOR_TASK,
            access_value=str(app_id),
            conversation_id=conversation_id,
            account=current_user,
            run_input=command.to_run_input(),
            command_id=command.command_id,
        )
        return json_response(result.model_dump(mode="json", exclude_none=True))

    @login_required
    def creator_task_stream(self, app_id: UUID, conversation_id: UUID) -> Response:
        """Creator 任务中心订阅 Agent 事件。"""
        subscription = WorkbenchSubscriptionReq.from_mapping(
            request.get_json(silent=True),
            last_event_id=request.headers.get("Last-Event-ID"),
        )
        events = self.service.subscribe(
            invoke_from=InvokeFrom.CREATOR_TASK,
            access_value=str(app_id),
            conversation_id=conversation_id,
            account=current_user,
            subscription=subscription,
        )
        return sse_response(events)

    @login_required
    def creator_task_state(self, app_id: UUID, conversation_id: UUID) -> Response:
        """Creator 任务中心读取会话状态。"""
        result = self.service.get_state(
            invoke_from=InvokeFrom.CREATOR_TASK,
            access_value=str(app_id),
            conversation_id=conversation_id,
            account=current_user,
        )
        response = WorkbenchStateResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def creator_task_messages(self, app_id: UUID, conversation_id: UUID) -> Response:
        """Creator 任务中心读取会话消息。"""
        query = CursorPaginatorReq(request.args)
        if not query.validate():
            return validate_error_json(query.errors)
        result = self.service.get_message_page(
            invoke_from=InvokeFrom.CREATOR_TASK,
            access_value=str(app_id),
            conversation_id=conversation_id,
            account=current_user,
            before=query.before_uuid,
            page_size=query.page_size.data,
        )
        response = WorkbenchMessagePageResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def creator_task_cancel_run(
        self, app_id: UUID, conversation_id: UUID, run_id: UUID
    ) -> Response:
        """Creator 任务中心取消指定 Agent Run。"""
        result = self.service.cancel_run(
            invoke_from=InvokeFrom.CREATOR_TASK,
            access_value=str(app_id),
            conversation_id=conversation_id,
            run_id=run_id,
            account=current_user,
        )
        response = WorkbenchCancelResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))


__all__ = ["AgentWorkbenchHandler"]
