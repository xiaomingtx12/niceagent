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
    WorkbenchEventStreamReq,
)
from internal.service import AgentWorkbenchService, ConversationService
from internal.service.agent_protocol.command_adapter import execute_command
from internal.service.agent.agent_workbench_query_service import AgentWorkbenchQueryService
from pkg.response import json_response, sse_response


@inject
@dataclass
class OpenAPIAgentProtocolHandler:
    """服务 API command、stream、state 和取消适配器。"""

    query_service: AgentWorkbenchQueryService
    service: AgentWorkbenchService
    conversation_service: ConversationService

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
        conversation_id = self.conversation_service.create_service_api_conversation(
            app_id=app_id,
            account=current_user,
            end_user_id=end_user_id,
        )
        return json_response({"conversation_id": str(conversation_id)})

    @login_required
    def command(self, app_id: UUID, conversation_id: UUID) -> Response:
        """鉴权后直接传递调用身份，客户端命令不能覆盖账号或来源。"""
        command = WorkbenchCommandReq.from_mapping(request.get_json(silent=True))
        conversation = self.conversation_service.get_service_api_conversation(
            app_id=app_id, conversation_id=conversation_id, account=current_user,
        )
        result = execute_command(
            self.service, app_id=conversation.app_id, conversation_id=conversation.id,
            invoke_from=InvokeFrom.SERVICE_API,
            account_id=current_user.id, created_by=conversation.created_by, end_user_id=conversation.created_by,
            command=command,
        )
        return json_response(result.model_dump(mode="json", exclude_none=True))

    @login_required
    def stream(self, app_id: UUID, conversation_id: UUID) -> Response:
        """先校验会话权限，再按已解析游标订阅当前入口的 Run。"""
        stream_request = WorkbenchEventStreamReq.from_mapping(
            request.get_json(silent=True),
            last_event_id=request.headers.get("Last-Event-ID"),
        )
        conversation = self.conversation_service.get_service_api_conversation(
            app_id=app_id, conversation_id=conversation_id, account=current_user,
        )
        events = self.query_service.subscribe(
            app_id=conversation.app_id, conversation_id=conversation.id,
            invoke_from=InvokeFrom.SERVICE_API, stream_request=stream_request,
        )
        return sse_response(events)

    @login_required
    def state(self, app_id: UUID, conversation_id: UUID) -> Response:
        """鉴权后只向状态查询传递会话 ID 和固定调用来源。"""
        conversation = self.conversation_service.get_service_api_conversation(
            app_id=app_id, conversation_id=conversation_id, account=current_user,
        )
        result = self.query_service.get_state(
            conversation_id=conversation.id, invoke_from=InvokeFrom.SERVICE_API,
        )
        return json_response(result.model_dump(mode="json"))

    @login_required
    def model_settings(self, app_id: UUID, conversation_id: UUID) -> Response:
        """模型选项使用当前账号和实际创建者身份判断可见范围。"""
        conversation = self.conversation_service.get_service_api_conversation(
            app_id=app_id, conversation_id=conversation_id, account=current_user,
        )
        result = self.query_service.get_model_settings(
            app_id=conversation.app_id, conversation_id=conversation.id,
            invoke_from=InvokeFrom.SERVICE_API,
            account_id=current_user.id, created_by=conversation.created_by,
        )
        return json_response(result.model_dump(mode="json"))

    @login_required
    def cancel_run(self, app_id: UUID, conversation_id: UUID, run_id: UUID) -> Response:
        """先校验会话权限，再限制目标 Run 的应用、会话和来源。"""
        conversation = self.conversation_service.get_service_api_conversation(
            app_id=app_id, conversation_id=conversation_id, account=current_user,
        )
        result = self.service.cancel_run(
            app_id=conversation.app_id, conversation_id=conversation.id,
            invoke_from=InvokeFrom.SERVICE_API, run_id=run_id,
        )
        response = WorkbenchCancelResp.model_validate(result, from_attributes=True)
        return json_response(response.model_dump(mode="json"))


__all__ = ["OpenAPIAgentProtocolHandler"]
