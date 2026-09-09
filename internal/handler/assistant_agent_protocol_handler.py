"""Creator 辅助 Agent 的 Agent Protocol HTTP 适配。"""

from dataclasses import dataclass
from uuid import UUID

from flask import Response, request
from flask_login import current_user, login_required
from injector import inject
from internal.schema.agent_workbench_read_schema import WorkbenchStateReq

from internal.schema import (
    WorkbenchCancelResp,
    WorkbenchCommandReq,
    WorkbenchStateResp,
    WorkbenchEventStreamReq,
)
from internal.service import AgentWorkbenchService, AssistantAgentService
from internal.middleware.rate_limit_middleware import rate_limit
from pkg.response import json_response, sse_response


@inject
@dataclass
class AssistantAgentProtocolHandler:
    """辅助 Agent command、stream、state 和取消适配器。"""

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
            context=self.assistant_agent_service.get_invocation_context(
                conversation_id=conversation_id,
                account=current_user,
            ),
            run_input=command.to_run_input(),
            command_id=command.command_id,
        )
        return json_response(result.model_dump(mode="json", exclude_none=True))

    @login_required
    def stream(self, conversation_id: UUID) -> Response:
        """订阅辅助 Agent 协议事件。"""
        stream_request = WorkbenchEventStreamReq.from_mapping(
            request.get_json(silent=True),
            last_event_id=request.headers.get("Last-Event-ID"),
        )
        events = self.service.subscribe(
            context=self.assistant_agent_service.get_invocation_context(
                conversation_id=conversation_id,
                account=current_user,
            ),
            stream_request=stream_request,
        )
        return sse_response(events)

    @login_required
    def state(self, conversation_id: UUID) -> Response:
        """读取辅助 Agent 状态。"""
        result = self.service.get_state(
            view=WorkbenchStateReq.from_query(request.args).view.data,
            context=self.assistant_agent_service.get_invocation_context(
                conversation_id=conversation_id,
                account=current_user,
            ),
        )
        response = WorkbenchStateResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))

    @login_required
    def model_settings(self, conversation_id: UUID) -> Response:
        """沿用辅助 Agent 会话权限读取模型选项。"""
        result = self.service.get_model_settings(context=self.assistant_agent_service.get_invocation_context(
            conversation_id=conversation_id, account=current_user,
        ))
        return json_response(result.model_dump(mode="json"))

    @login_required
    def cancel_run(self, conversation_id: UUID, run_id: UUID) -> Response:
        """取消辅助 Agent 的指定 Run。"""
        result = self.service.cancel_run(
            context=self.assistant_agent_service.get_invocation_context(
                conversation_id=conversation_id,
                account=current_user,
            ),
            run_id=run_id,
        )
        response = WorkbenchCancelResp.model_validate(result)
        return json_response(response.model_dump(mode="json"))


__all__ = ["AssistantAgentProtocolHandler"]
