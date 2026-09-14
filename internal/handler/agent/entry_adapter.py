"""Agent 各入口共用的请求解析与响应包装。

六个入口的命令、订阅、状态、模型选项和取消行为完全一致，差异只有会话鉴权方式
和 ``InvokeFrom``；本模块把这部分收敛到一处，避免同一段协议处理被复制多份。
放在 ``handler/agent`` 而非 ``handler/agent_protocol``：它被工作台入口与 Agent
Protocol 入口共同使用，属于通用入口设施，不能反过来依赖专用入口包。
"""

from dataclasses import dataclass
from uuid import UUID

from flask import Response, request

from internal.entity.conversation_entity import InvokeFrom
from internal.schema.agent.agent_workbench_schema import (
    WorkbenchCommandReq,
    WorkbenchEventStreamReq,
)
from internal.service.agent.agent_run_query import AgentRunQuery
from internal.service.agent.agent_run_service import AgentRunService
from internal.service.agent_protocol.command_adapter import execute_command
from pkg.response import json_response, sse_response


@dataclass(frozen=True)
class AgentRunCaller:
    """一次入口请求已解析的可信身份；客户端命令不能覆盖其中任何字段。"""

    app_id: UUID
    conversation_id: UUID
    invoke_from: InvokeFrom
    account_id: UUID
    created_by: UUID
    end_user_id: UUID | None = None


def respond_command(service: AgentRunService, caller: AgentRunCaller) -> Response:
    """解析命令并分派业务操作，命令 ID 只进入响应外壳。"""
    command = WorkbenchCommandReq.from_mapping(request.get_json(silent=True))
    result = execute_command(
        service, app_id=caller.app_id, conversation_id=caller.conversation_id,
        invoke_from=caller.invoke_from, account_id=caller.account_id,
        created_by=caller.created_by, end_user_id=caller.end_user_id, command=command,
    )
    return json_response(result.model_dump(mode="json", exclude_none=True))


def respond_stream(query_service: AgentRunQuery, caller: AgentRunCaller) -> Response:
    """按已解析游标订阅当前入口的 Run；游标同时接受 Last-Event-ID 头。"""
    stream_request = WorkbenchEventStreamReq.from_mapping(
        request.get_json(silent=True),
        last_event_id=request.headers.get("Last-Event-ID"),
    )
    events = query_service.stream_events(
        app_id=caller.app_id, conversation_id=caller.conversation_id,
        invoke_from=caller.invoke_from, stream_request=stream_request,
    )
    return sse_response(events)


def respond_state(query_service: AgentRunQuery, caller: AgentRunCaller) -> Response:
    """返回当前入口的状态快照，只读已提交数据。"""
    result = query_service.get_state(
        conversation_id=caller.conversation_id, invoke_from=caller.invoke_from,
    )
    return json_response(result.model_dump(mode="json"))


def respond_model_settings(query_service: AgentRunQuery, caller: AgentRunCaller) -> Response:
    """返回模型选项；可见范围由查询服务按账号与创建者身份判断。"""
    result = query_service.get_model_settings(
        app_id=caller.app_id, conversation_id=caller.conversation_id,
        invoke_from=caller.invoke_from, account_id=caller.account_id,
        created_by=caller.created_by,
    )
    return json_response(result.model_dump(mode="json"))


def respond_cancel(service: AgentRunService, caller: AgentRunCaller, run_id: UUID) -> Response:
    """取消指定 Run 并返回真实状态；等待表单的任务会被直接取消。"""
    result = service.cancel_run(
        app_id=caller.app_id, conversation_id=caller.conversation_id,
        invoke_from=caller.invoke_from, run_id=run_id,
    )
    return json_response(result.model_dump(mode="json"))


__all__ = [
    "AgentRunCaller",
    "respond_cancel",
    "respond_command",
    "respond_model_settings",
    "respond_state",
    "respond_stream",
]
