"""Agent 各入口共用的请求解析与响应包装。

六个入口的命令、订阅、状态、模型选项和取消行为完全一致，差异只有会话鉴权方式
和 ``InvokeFrom``；本模块把这部分收敛到一处，避免同一段协议处理被复制多份。
放在 ``handler/agent`` 而非 ``handler/agent_protocol``：它被工作台入口与 Agent
Protocol 入口共同使用，属于通用入口设施，不能反过来依赖专用入口包。
"""

from dataclasses import dataclass
from uuid import UUID

from flask import Response, request

from internal.entity.agent_execution_interaction import ExecutionRejected
from internal.entity.conversation_entity import InvokeFrom
from internal.schema.agent.agent_protocol_schema import (
    AgentProtocolCommandErrorResp,
    AgentProtocolCommandReq,
    AgentProtocolCommandSuccessResp,
    AgentProtocolEventStreamReq,
    AgentProtocolInputRespondReq,
)
from internal.service.agent.agent_execution_query import AgentExecutionQuery
from internal.service.agent.agent_execution_service import AgentExecutionService
from pkg.response import json_response, sse_response


@dataclass(frozen=True)
class AgentExecutionCaller:
    """一次入口请求已解析的可信身份；客户端命令不能覆盖其中任何字段。"""

    app_id: UUID
    conversation_id: UUID
    invoke_from: InvokeFrom
    account_id: UUID
    created_by: UUID
    end_user_id: UUID | None = None


def respond_command(service: AgentExecutionService, caller: AgentExecutionCaller) -> Response:
    """解析命令并按类型分派业务操作，命令 ID 只进入响应外壳。"""
    command = AgentProtocolCommandReq.from_mapping(request.get_json(silent=True))
    params = command.params
    if isinstance(params, AgentProtocolInputRespondReq):
        result = service.submit_answers(
            account_id=caller.account_id, app_id=caller.app_id,
            conversation_id=caller.conversation_id, created_by=caller.created_by,
            end_user_id=caller.end_user_id, invoke_from=caller.invoke_from,
            answers={item.interrupt_id: item.response for item in params.responses},
        )
    else:
        result = service.start_run(
            account_id=caller.account_id, app_id=caller.app_id,
            conversation_id=caller.conversation_id, created_by=caller.created_by,
            end_user_id=caller.end_user_id, invoke_from=caller.invoke_from,
            run_input=params,
        )
    if isinstance(result, ExecutionRejected):
        response: AgentProtocolCommandErrorResp | AgentProtocolCommandSuccessResp = (
            AgentProtocolCommandErrorResp(
                id=command.id, message=result.message, field_errors=result.field_errors,
            )
        )
    else:
        response = AgentProtocolCommandSuccessResp.create(
            command_id=command.id, run_id=result.run_id,
            applied_through_sequence=result.sequence,
        )
    return json_response(response.model_dump(mode="json", exclude_none=True))


def respond_stream(query_service: AgentExecutionQuery, caller: AgentExecutionCaller) -> Response:
    """按已解析游标订阅当前入口的 Run；游标同时接受 Last-Event-ID 头。"""
    stream_request = AgentProtocolEventStreamReq.from_mapping(
        request.get_json(silent=True),
        last_event_id=request.headers.get("Last-Event-ID"),
    )
    events = query_service.stream_events(
        app_id=caller.app_id, conversation_id=caller.conversation_id,
        invoke_from=caller.invoke_from, stream_request=stream_request,
    )
    return sse_response(events)


def respond_state(query_service: AgentExecutionQuery, caller: AgentExecutionCaller) -> Response:
    """返回当前入口的状态快照，只读已提交数据。"""
    result = query_service.get_state(
        conversation_id=caller.conversation_id, invoke_from=caller.invoke_from,
    )
    return json_response(result.model_dump(mode="json"))


def respond_model_settings(query_service: AgentExecutionQuery, caller: AgentExecutionCaller) -> Response:
    """返回会话模型设置与切换权限；账号模型目录由独立接口读取。"""
    result = query_service.get_model_settings(
        app_id=caller.app_id, conversation_id=caller.conversation_id,
        invoke_from=caller.invoke_from, account_id=caller.account_id,
        created_by=caller.created_by,
    )
    return json_response(result.model_dump(mode="json"))


def respond_summary(query_service: AgentExecutionQuery, caller: AgentExecutionCaller) -> Response:
    """使用与 state 相同的授权结果；摘要正文不进入 state、snapshot 或 SSE。"""
    result = query_service.get_summary(conversation_id=caller.conversation_id)
    response = json_response(result.model_dump(mode="json"))
    response.headers["Cache-Control"] = "no-store"
    return response


def respond_cancel(service: AgentExecutionService, caller: AgentExecutionCaller, run_id: UUID) -> Response:
    """取消指定 Run 并返回真实状态；等待表单的任务会被直接取消。"""
    result = service.cancel_run(
        app_id=caller.app_id, conversation_id=caller.conversation_id,
        invoke_from=caller.invoke_from, run_id=run_id,
    )
    return json_response(result.model_dump(mode="json"))


__all__ = [
    "AgentExecutionCaller",
    "respond_cancel",
    "respond_command",
    "respond_model_settings",
    "respond_state",
    "respond_summary",
    "respond_stream",
]
