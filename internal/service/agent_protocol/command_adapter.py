"""SDK 命令分派和响应适配；业务服务只接收可信输入并返回业务结果。"""

from typing import TYPE_CHECKING

from pydantic import ValidationError

from internal.entity.agent_run_interaction import RunAccepted, RunRejected
from internal.exception import ValidateErrorException
from internal.entity.agent_invocation import (
    AgentInvocationContext,
)
from internal.schema.agent.agent_workbench_schema import (
    WorkbenchCommandReq, WorkbenchCommandSuccessResp, WorkbenchCommandErrorResp,
    WorkbenchInputRespondReq, WorkbenchInputReopenReq,
)

if TYPE_CHECKING:
    from internal.service.agent.agent_workbench_service import AgentWorkbenchService


def execute_command(
    service: "AgentWorkbenchService",
    *,
    context: AgentInvocationContext,
    command: WorkbenchCommandReq,
    user_input_enabled: bool = True,
) -> WorkbenchCommandSuccessResp | WorkbenchCommandErrorResp:
    """按 SDK 方法选择业务入口，命令 ID 只参与响应，不进入运行状态。"""
    if command.method == "input.respond":
        try:
            response = WorkbenchInputRespondReq.model_validate(command.params)
        except ValidationError as exc:
            raise ValidateErrorException("表单提交格式错误") from exc
        result = service.respond_input(
            context=context,
            answers={item.interrupt_id: item.response for item in response.responses},
        )
    elif command.method == "input.reopen":
        try:
            request = WorkbenchInputReopenReq.model_validate(command.params)
        except ValidationError as exc:
            raise ValidateErrorException("表单恢复格式错误") from exc
        result = service.reopen_input(
            context=context, run_id=request.run_id,
            cancellation_sequence=request.cancellation_sequence,
        )
    else:
        result = service.start_run(
            context=context, run_input=command.input_data().to_run_input(),
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
