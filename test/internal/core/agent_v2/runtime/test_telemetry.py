"""Structured Agent runtime telemetry tests."""

from uuid import uuid4

from internal.core.agent_v2.entities.queue_entity import (
    AgentThought,
    QueueEvent,
)
from internal.core.agent_v2.events.session import EventTranslationSession
from internal.core.agent_v2.runtime.telemetry import (
    AgentTelemetry,
    AgentTelemetryEvent,
)


class _RecordingSink:
    """保存测试观察到的遥测事件。"""

    def __init__(self):
        self.events = []

    def record(self, event: AgentTelemetryEvent) -> None:
        """记录一个事件。"""
        self.events.append(event)


def test_telemetry_merges_scope_and_drops_non_scalar_attributes():
    """Metric adapters receive stable scope and scalar metadata only."""
    sink = _RecordingSink()
    telemetry = AgentTelemetry(
        sink,
        attributes={"account_id": "acc", "empty": None},
    )

    telemetry.emit(
        "workspace.write",
        value=3,
        attributes={"size_bytes": 3, "content": {"secret": True}},
    )

    event = sink.events[0]
    assert event.name == "workspace.write"
    assert event.value == 3
    assert event.attributes == {"account_id": "acc", "size_bytes": 3}


def test_event_session_emits_metadata_without_message_content():
    """Business event telemetry excludes answer and observation bodies."""
    sink = _RecordingSink()
    published = []
    session = EventTranslationSession(
        task_id=uuid4(),
        publisher=lambda task_id, thought: published.append(thought),
        telemetry=AgentTelemetry(sink),
    )

    session.publish(
        AgentThought(
            id=uuid4(),
            task_id=session.task_id,
            event=QueueEvent.AGENT_MESSAGE,
            answer="private answer",
            total_token_count=7,
        )
    )

    event = sink.events[0]
    assert event.name == "agent.event"
    assert event.attributes["event"] == "agent_message"
    assert event.attributes["total_tokens"] == 7
    assert "private answer" not in str(event.to_dict())
