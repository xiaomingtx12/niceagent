"""应用调试入口的 Agent 命令、状态与事件订阅。"""

from dataclasses import dataclass
from uuid import UUID

from flask import Response
from flask_login import current_user, login_required
from injector import inject

from internal.entity.conversation_entity import InvokeFrom
from internal.handler.agent.entry_adapter import (
    AgentExecutionCaller,
    respond_cancel,
    respond_command,
    respond_model_settings,
    respond_state,
    respond_stream,
)
from internal.service import AgentExecutionService, AppService
from internal.service.agent.agent_execution_query import AgentExecutionQuery


@inject
@dataclass
class DebugAgentHandler:
    """调试入口只使用本入口的会话授权，执行和查询复用工作台能力。"""

    service: AgentExecutionService
    query_service: AgentExecutionQuery
    app_service: AppService

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

    def _caller(self, app_id: UUID, conversation_id: UUID) -> AgentExecutionCaller:
        """五个动作共用同一次授权；调试入口的创建者就是当前账号。"""
        conversation = self.app_service.get_debugger_conversation(
            app_id=app_id, conversation_id=conversation_id, account=current_user,
        )
        return AgentExecutionCaller(
            app_id=conversation.app_id, conversation_id=conversation.id,
            invoke_from=InvokeFrom.DEBUGGER,
            account_id=current_user.id, created_by=current_user.id,
        )


__all__ = ["DebugAgentHandler"]
