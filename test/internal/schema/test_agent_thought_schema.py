"""Shared persisted Agent trace serializer tests."""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

from internal.schema.agent_thought_schema import dump_agent_thought


def test_dump_agent_thought_includes_trace_and_todo_fields():
    """All conversation schemas expose the same P2 trace contract."""
    trace = SimpleNamespace(
        id=uuid4(),
        position=3,
        event="todo_update",
        thought="",
        observation="",
        tool="write_todos",
        tool_input={"todos": []},
        agent_id="main",
        agent_type="main",
        parent_agent_id=None,
        delegation_id=None,
        task_id=None,
        todos=[{"content": "完成实现", "status": "completed"}],
        latency=0.2,
        created_at=datetime(2026, 7, 28, 12, 0, 0),
    )

    dumped = dump_agent_thought(trace)

    assert dumped["agent_id"] == "main"
    assert dumped["agent_type"] == "main"
    assert dumped["todos"] == [
        {"content": "完成实现", "status": "completed"}
    ]
    assert dumped["created_at"] > 0

