"""开放 API 的 Agent Protocol HTTP 适配。"""

from dataclasses import dataclass
from uuid import UUID

from flask import Response, request
from flask_login import current_user, login_required
from injector import inject

from internal.entity.conversation_entity import InvokeFrom
from internal.exception import ValidateErrorException
from internal.schema import (
    WorkbenchCancelResp,
    WorkbenchCommandReq,
    WorkbenchMessagePageResp,
    WorkbenchStateResp,
    WorkbenchSubscriptionReq,
)
from internal.service import AgentWorkbenchService
from pkg.paginator import CursorPaginatorReq
from pkg.response import json_response, sse_response, validate_error_json


@inject
@dataclass
class OpenAPIAgentProtocolHandler:
    """服务 API command、stream、state 和 messages 适配器。"""

    service: AgentWorkbenchService

    @login_required
    def create_conversation(self, app_id: UUID) -> Response:
        """创建开放 API 会话并返回后端分配的会话 ID。"""
        body = request.get_json(silent=True)
        end_user_id = None
        if isinstance(body, dict) and body.get("end_user_id"):
            try:
                end_user_id = UUID(str(body["end_user_id"]))
            except (TypeError, ValueError) as exc:
                raise ValidateErrorException("end_user_id 必须是有效 UUID") from exc
        conversation_id = self.service.create_service_api_conversation(
            app_id=app_id,
            account=current_user,
            end_user_id=end_user_id,
        )
        return json_response({"conversation_id": str(conversation_id)})

    @login_required
    def command(self, app_id: UUID, conversation_id: UUID) -> Response:
        """提交开放 API 的 Agent Protocol 命令。"""
        body = request.get_json(silent=True)
        command = WorkbenchCommandReq.from_mapping(body)
        result = self.service.start_run(
            invoke_from=InvokeFrom.SERVICE_API,
            access_value=str(app_id),
            conversation_id=conversation_id,
            account=current_user,
            run_input=command.to_run_input(),
            command_id=command.command_id,
        )
        return json_response(result.model_dump(mode="json", exclude_none=True))

    @login_required
    def stream(self, app_id: UUID, conversation_id: UUID) -> Response:
        """订阅开放 API 的 Agent Protocol 事件。"""
        subscription = WorkbenchSubscriptionReq.from_mapping(
            request.get_json(silent=True),
            last_event_id=request.headers.get("Last-Event-ID"),
        )
        events = self.service.subscribe(
            invoke_from=InvokeFrom.SERVICE_API,
            access_value=str(app_id),
            conversation_id=conversation_id,
            account=current_user,
            subscription=subscription,
        )
        return sse_response(events)

    @login_required
    def state(self, app_id: UUID, conversation_id: UUID) -> Response:
        """读取开放 API 会话状态。"""
        result = self.service.get_state(
            invoke_from=InvokeFrom.SERVICE_API,
            access_value=str(app_id),
            conversation_id=conversation_id,
            account=current_user,
        )
        response = WorkbenchStateResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def messages(self, app_id: UUID, conversation_id: UUID) -> Response:
        """读取开放 API 历史消息。"""
        query = CursorPaginatorReq(request.args)
        if not query.validate():
            return validate_error_json(query.errors)
        result = self.service.get_message_page(
            invoke_from=InvokeFrom.SERVICE_API,
            access_value=str(app_id),
            conversation_id=conversation_id,
            account=current_user,
            before=query.before_uuid,
            page_size=query.page_size.data,
        )
        response = WorkbenchMessagePageResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def cancel_run(
        self,
        app_id: UUID,
        conversation_id: UUID,
        run_id: UUID,
    ) -> Response:
        """取消开放 API 的指定 Agent Run。"""
        result = self.service.cancel_run(
            invoke_from=InvokeFrom.SERVICE_API,
            access_value=str(app_id),
            conversation_id=conversation_id,
            run_id=run_id,
            account=current_user,
        )
        response = WorkbenchCancelResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))


__all__ = ["OpenAPIAgentProtocolHandler"]
