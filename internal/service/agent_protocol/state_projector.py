"""将持久化快照与历史事件合并为客户端恢复状态，不访问数据库。"""

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from internal.entity import WORKBENCH_TERMINAL_STATUSES
from internal.model import AgentRun
from internal.schema.agent.agent_workbench_schema import WorkbenchEvent

_PUBLIC_STATE_KEYS = ("todos", "files", "async_tasks", "pending_inputs", "input_history", "input_recovery")


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
