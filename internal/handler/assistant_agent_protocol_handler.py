"""Creator 辅助 Agent 的 Agent Protocol HTTP 适配。"""

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
    WorkbenchStateResp,
    WorkbenchSubscriptionReq,
)
from internal.service import AgentWorkbenchService, AssistantAgentService
from internal.middleware.rate_limit_middleware import rate_limit
from pkg.paginator import CursorPaginatorReq
from pkg.response import json_response, sse_response, validate_error_json


@inject
@dataclass
class AssistantAgentProtocolHandler:
    """辅助 Agent command、stream、state 和 messages 适配器。"""

    service: AgentWorkbenchService
    assistant_agent_service: AssistantAgentService

    @rate_limit(config_key="user")
    @login_required
    def create_conversation(self) -> Response:
        """创建或复用当前账号的辅助 Agent 会话。"""
        conversation = self.assistant_agent_service.get_or_create_conversation(
            current_user
        )
        return json_response({"conversation_id": str(conversation.id)})

    @login_required
    def command(self, conversation_id: UUID) -> Response:
        """提交辅助 Agent 协议命令。"""
        body = request.get_json(silent=True)
        command = WorkbenchCommandReq.from_mapping(body)
        result = self.service.start_run(
            invoke_from=InvokeFrom.ASSISTANT_AGENT,
            access_value="assistant-agent",
            conversation_id=conversation_id,
            account=current_user,
            run_input=command.to_run_input(),
            command_id=command.command_id,
        )
        return json_response(result.model_dump(mode="json", exclude_none=True))

    @login_required
    def stream(self, conversation_id: UUID) -> Response:
        """订阅辅助 Agent 协议事件。"""
        subscription = WorkbenchSubscriptionReq.from_mapping(
            request.get_json(silent=True),
            last_event_id=request.headers.get("Last-Event-ID"),
        )
        events = self.service.subscribe(
            invoke_from=InvokeFrom.ASSISTANT_AGENT,
            access_value="assistant-agent",
            conversation_id=conversation_id,
            account=current_user,
            subscription=subscription,
        )
        return sse_response(events)

    @login_required
    def state(self, conversation_id: UUID) -> Response:
        """读取辅助 Agent 状态。"""
        result = self.service.get_state(
            invoke_from=InvokeFrom.ASSISTANT_AGENT,
            access_value="assistant-agent",
            conversation_id=conversation_id,
            account=current_user,
        )
        response = WorkbenchStateResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def messages(self, conversation_id: UUID) -> Response:
        """读取辅助 Agent 历史消息。"""
        query = CursorPaginatorReq(request.args)
        if not query.validate():
            return validate_error_json(query.errors)
        result = self.service.get_message_page(
            invoke_from=InvokeFrom.ASSISTANT_AGENT,
            access_value="assistant-agent",
            conversation_id=conversation_id,
            account=current_user,
            before=query.before_uuid,
            page_size=query.page_size.data,
        )
        response = WorkbenchMessagePageResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def cancel_run(self, conversation_id: UUID, run_id: UUID) -> Response:
        """取消辅助 Agent 的指定 Run。"""
        result = self.service.cancel_run(
            invoke_from=InvokeFrom.ASSISTANT_AGENT,
            access_value="assistant-agent",
            conversation_id=conversation_id,
            run_id=run_id,
            account=current_user,
        )
        response = WorkbenchCancelResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))


__all__ = ["AssistantAgentProtocolHandler"]
