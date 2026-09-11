"""将持久化快照与历史事件合并为客户端恢复状态，不访问数据库。"""

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from internal.core.agent_v2.entity.execution_result import AgentExecutionResult
from internal.core.agent_v2.entity.user_input import PendingUserInput
from internal.entity import WORKBENCH_TERMINAL_STATUSES, WorkbenchRunStatus
from internal.model import AgentRun
from internal.schema.agent.agent_workbench_schema import WorkbenchEvent
from .event_projector import project_public_state

_PUBLIC_STATE_KEYS = ("todos", "files", "async_tasks", "pending_inputs", "input_history", "input_recovery")


def project_execution_state(
    result: AgentExecutionResult, previous_snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """只公开执行结果的展示状态和已校验表单，答案历史沿用平台已提交快照。"""
    state = project_public_state({
        key: result.state[key] for key in ("todos", "files", "async_tasks")
        if key in result.state
    })
    state.update(
        pending_inputs=[
            PendingUserInput.model_validate(item).model_dump(mode="json")
            for item in result.interrupts
        ],
        input_history=list((previous_snapshot or {}).get("input_history", [])),
        input_recovery=None,
    )
    return state


def project_execution_lifecycle(
    status: WorkbenchRunStatus,
    *,
    message_id: UUID,
    input_message_id: str | None,
    public_error: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """将执行结束状态映射为实时生命周期；业务层仅传入可公开的错误信息。"""
    data: dict[str, Any] = {
        "event": status.value,
        "status": status.value,
        "message_id": str(message_id),
        "input_message_id": input_message_id,
    }
    if status is WorkbenchRunStatus.WAITING_INPUT:
        data.update(event="interrupted", reason="user_input_required")
    elif status is WorkbenchRunStatus.CANCELLED:
        data.update(event="interrupted", reason="cancel_requested")
    elif status is WorkbenchRunStatus.FAILED:
        data["error"] = public_error or "Agent 运行失败"
        if error_code is not None:
            data["code"] = error_code
    return data


def project_snapshot_values(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """只公开会话状态，过滤模型调用配置和内部表单恢复信息。"""
    source = snapshot or {}
    return {key: source[key] for key in _PUBLIC_STATE_KEYS if key in source}


def build_state_snapshot_event(
    run_id: UUID, sequence: int, snapshot: Mapping[str, Any] | None,
) -> WorkbenchEvent | None:
    """合成事件补齐历史状态，复用已提交序号，不推进 SSE 游标。"""
    data = project_snapshot_values(snapshot)
    if not data:
        return None
    return WorkbenchEvent(
        type="event",
        event_id=f"{run_id}:state-snapshot",
        seq=max(0, sequence),
        method="values",
        params={
            "namespace": [],
            "timestamp": None,
            "data": data,
            "metadata": {"my_dify": {"source": "state_hydration", "run_id": str(run_id)}},
        },
    )


def project_run_history(
    runs: Sequence[AgentRun], events_by_run: Mapping[UUID, Sequence[WorkbenchEvent]],
) -> list[WorkbenchEvent]:
    """历史事件提供过程，数据库快照和终态负责补齐最终结果。"""
    events: list[WorkbenchEvent] = []
    for run in runs:
        events.extend(events_by_run.get(run.id, ()))
        sequence = int(run.last_event_sequence or 0)
        snapshot = build_state_snapshot_event(run.id, sequence, run.state_snapshot)
        if snapshot is not None:
            events.append(snapshot)
        if run.status in WORKBENCH_TERMINAL_STATUSES:
            events.append(_build_terminal_event(run, sequence))
    return sorted(events, key=lambda event: event.seq)


def project_pending_tasks(
    status: str,
    values: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """把待输入快照转换为工作台任务，保持客户端表单协议结构。"""
    if status != "waiting_input":
        return []

    tasks = []
    for item in values.get("pending_inputs", []):
        interrupt_id = item["interrupt_id"]
        interrupt = {
            "id": interrupt_id,
            "value": item["payload"],
            "ns": item.get("namespace", []),
        }
        tasks.append(
            {
                "id": interrupt_id,
                "name": "ask_user",
                "interrupts": [interrupt],
            }
        )
    return tasks


def _build_terminal_event(run: AgentRun, sequence: int) -> WorkbenchEvent:
    """从权威终态恢复生命周期事件；内部错误详情不进入公开响应。"""
    data = {
        "event": "interrupted" if run.status == "cancelled" else run.status,
        "status": run.status,
        "message_id": str(run.message_id),
        "input_message_id": run.input_message_id,
    }
    if run.status == "failed":
        data["error"] = "Agent 运行失败"
    return WorkbenchEvent(
        type="event",
        event_id=f"{run.id}:terminal-hydration",
        seq=sequence,
        method="lifecycle",
        params={"data": data},
    )
