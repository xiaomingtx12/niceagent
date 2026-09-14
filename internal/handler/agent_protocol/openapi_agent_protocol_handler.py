"""开放 API 的 Agent Protocol HTTP 适配。"""

from dataclasses import dataclass
from uuid import UUID

from flask import Response, request
from flask_login import current_user, login_required
from injector import inject

from internal.entity.conversation_entity import InvokeFrom
from internal.exception import ValidateErrorException
from internal.handler.agent.entry_adapter import (
    AgentRunCaller,
    respond_cancel,
    respond_command,
    respond_model_settings,
    respond_state,
    respond_stream,
)
from internal.service import AgentRunService, ConversationService
from internal.service.agent.agent_run_query import AgentRunQuery
from pkg.response import json_response


@inject
@dataclass
class OpenAPIAgentProtocolHandler:
    """服务 API command、stream、state 和取消适配器。"""

    query_service: AgentRunQuery
    service: AgentRunService
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
        return respond_command(self.service, self._caller(app_id, conversation_id))

    @login_required
    def stream(self, app_id: UUID, conversation_id: UUID) -> Response:
        """先校验会话权限，再按已解析游标订阅当前入口的 Run。"""
        return respond_stream(self.query_service, self._caller(app_id, conversation_id))

    @login_required
    def state(self, app_id: UUID, conversation_id: UUID) -> Response:
        """鉴权后只向状态查询传递会话 ID 和固定调用来源。"""
        return respond_state(self.query_service, self._caller(app_id, conversation_id))

    @login_required
    def model_settings(self, app_id: UUID, conversation_id: UUID) -> Response:
        """模型选项使用当前账号和实际创建者身份判断可见范围。"""
        return respond_model_settings(self.query_service, self._caller(app_id, conversation_id))

    @login_required
    def cancel_run(self, app_id: UUID, conversation_id: UUID, run_id: UUID) -> Response:
        """先校验会话权限，再限制目标 Run 的应用、会话和来源。"""
        return respond_cancel(self.service, self._caller(app_id, conversation_id), run_id)

    def _caller(self, app_id: UUID, conversation_id: UUID) -> AgentRunCaller:
        """五个动作共用同一次授权；服务 API 的创建者是会话所有者。"""
        conversation = self.conversation_service.get_service_api_conversation(
            app_id=app_id, conversation_id=conversation_id, account=current_user,
        )
        return AgentRunCaller(
            app_id=conversation.app_id, conversation_id=conversation.id,
            invoke_from=InvokeFrom.SERVICE_API,
            account_id=current_user.id, created_by=conversation.created_by,
            end_user_id=conversation.created_by,
        )


__all__ = ["OpenAPIAgentProtocolHandler"]
