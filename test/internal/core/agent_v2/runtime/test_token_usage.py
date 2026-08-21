"""Token 用量归一化和运行聚合测试。"""

from uuid import uuid4

from langchain_core.messages import AIMessage

from internal.core.agent_v2.events.session import EventTranslationSession
from internal.core.agent_v2.runtime.telemetry import AgentTelemetry, AgentTelemetryEvent
from internal.core.agent_v2.runtime.token_usage import (
    TokenUsageAccumulator,
    normalize_message,
    normalize_usage,
)


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AgentTelemetryEvent] = []

    def record(self, event: AgentTelemetryEvent) -> None:
        self.events.append(event)


def test_normalizer_reads_provider_aliases_and_detail_tokens():
    usage = normalize_message(
        AIMessage(
            content="done",
            response_metadata={
                "token_usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 4,
                    "total_tokens": 12,
                    "prompt_tokens_details": {"cached_tokens": 3},
                    "completion_tokens_details": {"reasoning_tokens": 2},
                }
            },
        )
    )

    assert usage.present is True
    assert usage.input_tokens == 8
    assert usage.output_tokens == 4
    assert usage.total_tokens == 12
    assert usage.input_details == {"cached_tokens": 3}
    assert usage.output_details == {"reasoning_tokens": 2}
    assert usage.source == "response_metadata.token_usage"
    assert usage.to_attributes()["input_cached_tokens"] == 3
    assert usage.to_attributes()["output_reasoning_tokens"] == 2


def test_accumulator_keeps_model_breakdown_and_computes_missing_total():
    aggregate = TokenUsageAccumulator()
    aggregate.add(
        normalize_usage(
            {"input_tokens": 5, "output_tokens": 2},
            source="usage_metadata",
        ),
        model_name="model-a",
    )
    aggregate.add(
        normalize_usage(
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            source="usage_metadata",
        ),
        model_name="model-b",
    )

    assert aggregate.call_count == 2
    assert aggregate.total.to_attributes()["total_tokens"] == 9
    assert aggregate.by_model["model-a"].to_attributes()["total_tokens"] == 7


def test_session_prefers_framework_callback_aggregate_and_emits_details():
    sink = _RecordingSink()
    session = EventTranslationSession(
        task_id=uuid4(),
        publisher=lambda _task_id, _thought: None,
        telemetry=AgentTelemetry(sink),
    )

    session.publish_token_usage(
        {
            "model-a": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "output_token_details": {"reasoning": 2},
            }
        }
    )

    run_event = next(
        event for event in sink.events if event.name == "agent.run.token_usage"
    )
    model_event = next(
        event for event in sink.events if event.name == "agent.model.token_usage"
    )
    assert run_event.attributes["usage_source"] == "langchain_callback"
    assert run_event.attributes["total_tokens"] == 15
    assert model_event.attributes["model_name"] == "model-a"
    assert model_event.attributes["output_reasoning_tokens"] == 2


def test_session_builds_frontend_usage_payload_with_call_counts():
    session = EventTranslationSession(
        task_id=uuid4(),
        publisher=lambda _task_id, _thought: None,
    )
    session.record_model_call()
    session.record_tool_call()

    payload = session.publish_token_usage(
        {
            "model-a": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "input_token_details": {"cached": 3},
            }
        }
    )

    assert payload is not None
    assert payload["model_call_count"] == 1
    assert payload["tool_call_count"] == 1
    assert payload["input_tokens"] == 10
    assert payload["output_tokens"] == 5
    assert payload["models"]["model-a"]["total_tokens"] == 15
