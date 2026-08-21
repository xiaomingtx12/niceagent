"""TraceMiddleware 生命周期和脱敏测试。"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, ToolMessage

from internal.core.agent_v2.events.session import bind_event_translation
from internal.core.agent_v2.middleware.trace_middleware import TraceMiddleware
from internal.core.agent_v2.runtime.telemetry import AgentTelemetry, AgentTelemetryEvent


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AgentTelemetryEvent] = []

    def record(self, event: AgentTelemetryEvent) -> None:
        self.events.append(event)


def _request(*, model=None, tool_call=None):
    return SimpleNamespace(
        model=model,
        tool_call=tool_call or {},
        tool=None,
        runtime=SimpleNamespace(
            context=SimpleNamespace(
                account_id="account-1",
                app_id="app-1",
                conversation_id="conversation-1",
                end_user_id=None,
                agent_id="main",
            )
        ),
    )


def test_model_span_records_duration_usage_and_shared_trace_id():
    sink = _RecordingSink()
    middleware = TraceMiddleware(AgentTelemetry(sink))
    published = []

    with bind_event_translation(
        task_id=uuid4(),
        publisher=lambda _task_id, thought: published.append(thought),
        telemetry=AgentTelemetry(sink),
    ) as session:
        response = middleware.wrap_model_call(
            _request(model=SimpleNamespace(model_name="gpt-test")),
            lambda _request: ModelResponse(
                result=[
                    AIMessage(
                        content="answer",
                        usage_metadata={
                            "input_tokens": 7,
                            "output_tokens": 3,
                            "total_tokens": 10,
                        },
                    )
                ]
            ),
        )

    assert isinstance(response, ModelResponse)
    events = [event for event in sink.events if event.name.startswith("agent.model.")]
    assert [event.name for event in events] == [
        "agent.model.started",
        "agent.model.finished",
    ]
    assert events[0].attributes["trace_id"] == str(session.trace_id)
    assert events[0].attributes["span_id"] == events[1].attributes["span_id"]
    assert events[1].attributes["status"] == "ok"
    assert events[1].attributes["duration_ms"] >= 0
    assert events[1].attributes["input_tokens"] == 7
    assert events[1].attributes["output_tokens"] == 3
    assert events[1].attributes["total_tokens"] == 10
    assert events[1].attributes["model_name"] == "gpt-test"
    assert events[1].attributes["agent_id"] == "main"


def test_model_failure_records_failed_span_and_re_raises():
    sink = _RecordingSink()
    middleware = TraceMiddleware(AgentTelemetry(sink))

    with bind_event_translation(
        task_id=uuid4(),
        publisher=lambda _task_id, _thought: None,
        telemetry=AgentTelemetry(sink),
    ):
        with pytest.raises(TimeoutError):
            middleware.wrap_model_call(
                _request(model=SimpleNamespace(name="slow-model")),
                lambda _request: (_ for _ in ()).throw(TimeoutError()),
            )

    event = next(event for event in sink.events if event.name == "agent.model.failed")
    assert event.attributes["status"] == "error"
    assert event.attributes["error_type"] == "TimeoutError"
    assert event.attributes["duration_ms"] >= 0


def test_tool_spans_record_success_and_failure_without_payload_leaks():
    sink = _RecordingSink()
    middleware = TraceMiddleware(AgentTelemetry(sink))
    tool_call = {
        "id": "tool-call-1",
        "name": "search",
        "args": {"query": "secret-query"},
    }

    with bind_event_translation(
        task_id=uuid4(),
        publisher=lambda _task_id, _thought: None,
        telemetry=AgentTelemetry(sink),
    ):
        middleware.wrap_tool_call(
            _request(tool_call=tool_call),
            lambda _request: ToolMessage(
                content="secret-result",
                tool_call_id="tool-call-1",
            ),
        )
        with pytest.raises(ValueError):
            middleware.wrap_tool_call(
                _request(
                    tool_call={
                        **tool_call,
                        "id": "tool-call-2",
                    }
                ),
                lambda _request: (_ for _ in ()).throw(ValueError("secret-error")),
            )

    tool_events = [event for event in sink.events if event.name.startswith("agent.tool.")]
    assert [event.name for event in tool_events] == [
        "agent.tool.started",
        "agent.tool.finished",
        "agent.tool.started",
        "agent.tool.failed",
    ]
    assert tool_events[1].attributes["tool_call_id"] == "tool-call-1"
    assert tool_events[3].attributes["error_type"] == "ValueError"
    assert "secret" not in str([event.to_dict() for event in tool_events])


def test_async_model_and_tool_hooks_emit_completion_events():
    sink = _RecordingSink()
    middleware = TraceMiddleware(AgentTelemetry(sink))

    async def run():
        with bind_event_translation(
            task_id=uuid4(),
            publisher=lambda _task_id, _thought: None,
            telemetry=AgentTelemetry(sink),
        ):
            await middleware.awrap_model_call(
                _request(model=SimpleNamespace(model="async-model")),
                async_handler_model,
            )
            await middleware.awrap_tool_call(
                _request(
                    tool_call={"id": "async-tool-call", "name": "lookup", "args": {}}
                ),
                async_handler_tool,
            )

    async def async_handler_model(_request):
        return ModelResponse(
            result=[
                AIMessage(
                    content="done",
                    usage_metadata={
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "total_tokens": 3,
                    },
                )
            ]
        )

    async def async_handler_tool(_request):
        return ToolMessage(content="done", tool_call_id="async-tool-call")

    asyncio.run(run())

    assert "agent.model.finished" in [event.name for event in sink.events]
    assert "agent.tool.finished" in [event.name for event in sink.events]
