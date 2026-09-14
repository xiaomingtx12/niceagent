"""SDK 命令分派和响应适配；业务服务只接收可信输入并返回业务结果。"""

from typing import TYPE_CHECKING
from uuid import UUID
from internal.entity.conversation_entity import InvokeFrom

from internal.entity.agent_run_interaction import RunAccepted, RunRejected
from internal.schema.agent.agent_workbench_schema import (
    WorkbenchCommandReq, WorkbenchCommandSuccessResp, WorkbenchCommandErrorResp,
    WorkbenchInputRespondReq, WorkbenchInputReopenReq,
)

if TYPE_CHECKING:
    from internal.service.agent.agent_run_service import AgentRunService


def execute_command(
    service: "AgentRunService", *, account_id: UUID, app_id: UUID, conversation_id: UUID,
    created_by: UUID, end_user_id: UUID | None = None, invoke_from: InvokeFrom,
    command: WorkbenchCommandReq, user_input_enabled: bool = True,
) -> WorkbenchCommandSuccessResp | WorkbenchCommandErrorResp:
    """按已解析参数分派业务操作，命令 ID 只用于响应，不进入运行状态。"""
    params = command.params
    if isinstance(params, WorkbenchInputRespondReq):
        result = service.submit_answers(
            account_id=account_id, app_id=app_id,
            conversation_id=conversation_id, created_by=created_by,
            end_user_id=end_user_id, invoke_from=invoke_from,
            answers={item.interrupt_id: item.response for item in params.responses},
        )
    elif isinstance(params, WorkbenchInputReopenReq):
        result = service.reopen_form(
            account_id=account_id, app_id=app_id,
            conversation_id=conversation_id, created_by=created_by,
            end_user_id=end_user_id, invoke_from=invoke_from, run_id=params.run_id,
            cancellation_sequence=params.cancellation_sequence,
        )
    else:
        result = service.start_run(
            account_id=account_id, app_id=app_id,
            conversation_id=conversation_id, created_by=created_by,
            end_user_id=end_user_id, invoke_from=invoke_from, run_input=params,
            user_input_enabled=user_input_enabled,
        )
    return command_response(result, command.id)


def command_response(
    result: RunAccepted | RunRejected, command_id: int | None,
) -> WorkbenchCommandSuccessResp | WorkbenchCommandErrorResp:
    """保留 SDK 成功、冲突和字段错误的现有响应格式。"""
    if isinstance(result, RunRejected):
        return WorkbenchCommandErrorResp(
            id=command_id, message=result.message, field_errors=result.field_errors,
        )
    return WorkbenchCommandSuccessResp.create(
        command_id=command_id, run_id=result.run_id,
        applied_through_sequence=result.sequence,
    )
