"""Agent stream result accumulation tests."""

from uuid import uuid4

from internal.core.agent_v2.agents.results import AgentResultAccumulator
from internal.core.agent_v2.entities.queue_entity import AgentThought, QueueEvent


def test_result_accumulator_merges_message_chunks_and_keeps_trace_events():
    """Message chunks merge while Todo and delegation events remain ordered."""
    event_id = uuid4()
    task_id = uuid4()
    accumulator = AgentResultAccumulator(query="问题", image_urls=[])
    accumulator.add(
        AgentThought(
            id=event_id,
            task_id=task_id,
            event=QueueEvent.AGENT_MESSAGE,
            answer="前半",
            latency=0.1,
        )
    )
    accumulator.add(
        AgentThought(
            id=event_id,
            task_id=task_id,
            event=QueueEvent.AGENT_MESSAGE,
            answer="后半",
            latency=0.2,
            message_token_count=2,
            answer_token_count=1,
            total_token_count=3,
            total_price=0.03,
        )
    )
    accumulator.add(
        AgentThought(
            id=uuid4(),
            task_id=task_id,
            event=QueueEvent.TODO_UPDATE,
            latency=0.05,
        )
    )

    result = accumulator.build()

    assert result.answer == "前半后半"
    assert result.agent_thoughts[0].answer == "前半后半"
    assert result.agent_thoughts[0].total_token_count == 3
    assert result.agent_thoughts[1].event == QueueEvent.TODO_UPDATE
    assert result.message_token_count == 2
    assert result.answer_token_count == 1
    assert result.total_token_count == 3
    assert result.total_price == 0.03
    assert result.latency == 0.25


def test_result_accumulator_records_terminal_error():
    """Error events update the non-streaming status and error message."""
    accumulator = AgentResultAccumulator(query="问题", image_urls=[])
    accumulator.add(
        AgentThought(
            id=uuid4(),
            task_id=uuid4(),
            event=QueueEvent.ERROR,
            observation="模型不可用",
        )
    )

    result = accumulator.build()

    assert result.status == QueueEvent.ERROR
    assert result.error == "模型不可用"
