"""将持久化快照与历史事件合并为客户端恢复状态，不访问数据库。"""

from collections.abc import Collection, Mapping, Sequence
from typing import Any
from uuid import UUID

from internal.core.agent_v2 import (
    AgentExecutionResult,
    PendingUserInput,
)
from internal.entity import WORKBENCH_TERMINAL_STATUSES, WorkbenchRunStatus
from internal.model import AgentRun
from internal.schema.agent.agent_workbench_schema import WorkbenchEvent
from copy import deepcopy

_PUBLIC_STATE_KEYS = ("todos", "files", "async_tasks", "pending_inputs", "input_history", "input_recovery")


def project_public_state(value: Mapping[str, Any]) -> dict[str, Any]:
    """绑定业务消息标识和异步会话字段；输入已由 Core 转为 JSON 兼容数据。"""
    projected = deepcopy(dict(value))
    # 会话摘要保留最后一条回答；阶段正文由公开 messages 事件独立重放。
    messages = projected.get("messages")
    if isinstance(messages, list):
        visible = []
        business_id = None
        run_id = None
        answer_index = None
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("type") or message.get("role")
            if role in {"human", "user"}:
                extra = message.get("additional_kwargs") or {}
                business_id = extra.get("business_message_id")
                run_id = extra.get("run_id")
                answer_index = None
                visible.append(message)
            elif role in {"ai", "assistant"} and business_id:
                message["id"] = f"{business_id}:assistant"
                message.setdefault("additional_kwargs", {})["run_id"] = run_id
                if answer_index is None:
                    answer_index = len(visible)
                    visible.append(message)
                else:
                    visible[answer_index] = message
            else:
                visible.append(message)
        projected["messages"] = visible
    async_tasks = projected.get("async_tasks")
    if not isinstance(async_tasks, dict):
        return projected
    for task in async_tasks.values():
        if not isinstance(task, dict):
            continue
        framework_thread_id = task.pop("thread_id", None)
        if framework_thread_id is not None:
            task.setdefault("conversation_id", framework_thread_id)
    return projected


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
    *, deferred_run_ids: Collection[UUID] = (),
) -> list[WorkbenchEvent]:
    """历史事件提供过程，数据库快照和终态负责补齐最终结果。"""
    events: list[WorkbenchEvent] = []
    for run in runs:
        history = events_by_run.get(run.id, ())
        sequence = int(run.last_event_sequence or 0)
        terminal = next((
            event for event in reversed(history)
            if run.status in WORKBENCH_TERMINAL_STATUSES
            and event.method == "lifecycle" and not event.params.namespace
            and event.seq == sequence and isinstance(event.params.data, Mapping)
            and event.params.data.get("status") == run.status
        ), None)
        events.extend(event for event in history if event is not terminal)
        snapshot = build_state_snapshot_event(run.id, sequence, run.state_snapshot)
        if snapshot is not None:
            events.append(snapshot)
        if run.status in WORKBENCH_TERMINAL_STATUSES:
            if terminal is None:
                terminal = _build_terminal_event(run, sequence)
            elif run.id in deferred_run_ids:
                # compact 用该标记过滤旧分片；真实错误、时间和其他字段保持不变。
                terminal = terminal.model_copy(update={"event_id": f"{run.id}:terminal-hydration"})
            events.append(terminal)
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
    data = project_execution_lifecycle(
        WorkbenchRunStatus(run.status), message_id=run.message_id,
        input_message_id=run.input_message_id,
    )
    return WorkbenchEvent(
        type="event",
        event_id=f"{run.id}:terminal-hydration",
        seq=sequence,
        method="lifecycle",
        params={"data": data},
    )
