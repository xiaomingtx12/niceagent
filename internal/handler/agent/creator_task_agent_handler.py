"""Creator 任务中心的 Agent 命令、状态与事件订阅。"""

from dataclasses import dataclass
from uuid import UUID

from flask import Response, request
from flask_login import current_user, login_required
from injector import inject

from internal.entity.conversation_entity import InvokeFrom

from internal.schema.agent.agent_workbench_schema import (
    WorkbenchCancelResp,
    WorkbenchCommandReq,
    WorkbenchEventStreamReq,
)
from internal.service import AgentWorkbenchService, CreatorTaskService
from internal.service.agent.agent_workbench_query_service import AgentWorkbenchQueryService
from internal.service.agent_protocol.command_adapter import execute_command
from pkg.response import json_response, sse_response


@inject
@dataclass
class CreatorTaskAgentHandler:
    """任务中心入口只使用本入口的会话授权，执行和查询复用工作台能力。"""

    service: AgentWorkbenchService
    query_service: AgentWorkbenchQueryService
    creator_task_service: CreatorTaskService

    @login_required
    def command(self, app_id: UUID, conversation_id: UUID) -> Response:
        """鉴权后直接传递调用身份，客户端命令不能覆盖账号或来源。"""
        command = WorkbenchCommandReq.from_mapping(request.get_json(silent=True))
        conversation = self.creator_task_service.get_owned_conversation(
            app_id=app_id, conversation_id=conversation_id, account=current_user,
        )
        result = execute_command(
            self.service, app_id=conversation.app_id, conversation_id=conversation.id,
            invoke_from=InvokeFrom.CREATOR_TASK,
            account_id=current_user.id, created_by=current_user.id,
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
        conversation = self.creator_task_service.get_owned_conversation(
            app_id=app_id, conversation_id=conversation_id, account=current_user,
        )
        events = self.query_service.subscribe(
            app_id=conversation.app_id, conversation_id=conversation.id,
            invoke_from=InvokeFrom.CREATOR_TASK, stream_request=stream_request,
        )
        return sse_response(events)

    @login_required
    def state(self, app_id: UUID, conversation_id: UUID) -> Response:
        """鉴权后只向状态查询传递会话 ID 和固定调用来源。"""
        conversation = self.creator_task_service.get_owned_conversation(
            app_id=app_id, conversation_id=conversation_id, account=current_user,
        )
        result = self.query_service.get_state(
            conversation_id=conversation.id, invoke_from=InvokeFrom.CREATOR_TASK,
        )
        return json_response(result.model_dump(mode="json"))

    @login_required
    def model_settings(self, app_id: UUID, conversation_id: UUID) -> Response:
        """模型选项使用当前账号和实际创建者身份判断可见范围。"""
        conversation = self.creator_task_service.get_owned_conversation(
            app_id=app_id, conversation_id=conversation_id, account=current_user,
        )
        result = self.query_service.get_model_settings(
            app_id=conversation.app_id, conversation_id=conversation.id,
            invoke_from=InvokeFrom.CREATOR_TASK,
            account_id=current_user.id, created_by=current_user.id,
        )
        return json_response(result.model_dump(mode="json"))

    @login_required
    def cancel_run(self, app_id: UUID, conversation_id: UUID, run_id: UUID) -> Response:
        """先校验会话权限，再限制目标 Run 的应用、会话和来源。"""
        conversation = self.creator_task_service.get_owned_conversation(
            app_id=app_id, conversation_id=conversation_id, account=current_user,
        )
        result = self.service.cancel_run(
            app_id=conversation.app_id, conversation_id=conversation.id,
            invoke_from=InvokeFrom.CREATOR_TASK, run_id=run_id,
        )
        response = WorkbenchCancelResp.model_validate(result, from_attributes=True)
        return json_response(response.model_dump(mode="json"))
