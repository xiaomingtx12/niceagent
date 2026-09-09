"""Project persisted state into public hydration events without database access."""

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from internal.entity import WORKBENCH_TERMINAL_STATUSES
from internal.model import AgentRun
from internal.schema.agent_workbench_schema import WorkbenchEvent

_PUBLIC_STATE_KEYS = ("todos", "files", "async_tasks", "pending_inputs", "input_history")


def project_snapshot_values(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep runtime invocation and interaction internals out of protocol state."""
    source = snapshot or {}
    return {key: source[key] for key in _PUBLIC_STATE_KEYS if key in source}


def build_state_snapshot_event(
    run_id: UUID, sequence: int, snapshot: Mapping[str, Any] | None,
) -> WorkbenchEvent | None:
    """Synthetic hydration events restore state without advancing the SSE cursor."""
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
    """Merge stored details with authoritative snapshots and terminal markers."""
    events: list[WorkbenchEvent] = []
    for run in runs:
        events.extend(events_by_run.get(run.id, ()))
        sequence = int(run.last_event_sequence or 0)
        snapshot = build_state_snapshot_event(run.id, sequence, run.state_snapshot)
        if snapshot is not None:
            events.append(snapshot)
        if run.status in WORKBENCH_TERMINAL_STATUSES:
            events.append(WorkbenchEvent(
                type="event",
                event_id=f"{run.id}:terminal-hydration",
                seq=sequence,
                method="lifecycle",
                params={"data": {
                    "event": "interrupted" if run.status == "cancelled" else run.status,
                    "status": run.status,
                    "message_id": str(run.message_id),
                    "input_message_id": run.input_message_id,
                    **({"error": "Agent 运行失败"} if run.status == "failed" else {}),
                }},
            ))
    return sorted(events, key=lambda event: event.seq)
