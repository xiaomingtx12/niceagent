"""Creator 辅助 Agent 的 Agent Protocol HTTP 适配。"""

from dataclasses import dataclass
from uuid import UUID

from flask import Response
from flask_login import current_user, login_required
from injector import inject

from internal.entity.conversation_entity import InvokeFrom
from internal.handler.agent.entry_adapter import (
    AgentRunCaller,
    respond_cancel,
    respond_command,
    respond_model_settings,
    respond_state,
    respond_stream,
)
from internal.service import AgentRunService, AssistantAgentService
from internal.middleware.rate_limit_middleware import rate_limit
from internal.service.agent.agent_run_query import AgentRunQuery
from pkg.response import json_response


@inject
@dataclass
class AssistantAgentProtocolHandler:
    """辅助 Agent command、stream、state 和取消适配器。"""

    query_service: AgentRunQuery
    service: AgentRunService
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
        """鉴权后直接传递调用身份，客户端命令不能覆盖账号或来源。"""
        return respond_command(self.service, self._caller(conversation_id))

    @login_required
    def stream(self, conversation_id: UUID) -> Response:
        """先校验会话权限，再按已解析游标订阅当前入口的 Run。"""
        return respond_stream(self.query_service, self._caller(conversation_id))

    @login_required
    def state(self, conversation_id: UUID) -> Response:
        """鉴权后只向状态查询传递会话 ID 和固定调用来源。"""
        return respond_state(self.query_service, self._caller(conversation_id))

    @login_required
    def model_settings(self, conversation_id: UUID) -> Response:
        """模型选项使用当前账号和实际创建者身份判断可见范围。"""
        return respond_model_settings(self.query_service, self._caller(conversation_id))

    @login_required
    def cancel_run(self, conversation_id: UUID, run_id: UUID) -> Response:
        """先校验会话权限，再限制目标 Run 的应用、会话和来源。"""
        return respond_cancel(self.service, self._caller(conversation_id), run_id)

    def _caller(self, conversation_id: UUID) -> AgentRunCaller:
        """五个动作共用同一次授权；内置助手应用由服务端按配置解析。"""
        conversation = self.assistant_agent_service.get_owned_conversation(
            conversation_id=conversation_id, account=current_user,
        )
        return AgentRunCaller(
            app_id=conversation.app_id, conversation_id=conversation.id,
            invoke_from=InvokeFrom.ASSISTANT_AGENT,
            account_id=current_user.id, created_by=current_user.id,
        )


__all__ = ["AssistantAgentProtocolHandler"]
