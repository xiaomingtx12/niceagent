"""工作台阶段输出与工具执行边界的回归测试，无外部模型或数据库调用。"""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from flask import Flask
from langchain_core.messages import AIMessageChunk, HumanMessage

from internal.core.agent_v2.entity.agent_event import AgentEvent
from internal.core.agent_v2.entity.execution_result import AgentExecutionResult
from internal.entity import WorkbenchRun
from internal.entity.conversation_entity import InvokeFrom
from internal.service.agent_protocol.event_projector import AgentProtocolEventProjector
from internal.service.agent_workbench_execution_service import AgentWorkbenchRunExecutionService


def test_partial_text_is_emitted_before_model_completion() -> None:
    """首个非终态 chunk 就必须产生浏览器可消费的文本，不等最终回答。"""
    projector = AgentProtocolEventProjector(root_message_id="answer")
    events = projector.project_many(AgentEvent(
        channel="messages", data=(AIMessageChunk(content="阶段一已完成", id="phase-one"), {}),
    ))
    assert [event["params"]["data"]["event"] for event in events] == [
        "message-start", "content-block-start", "content-block-delta",
    ]
    assert events[-1]["params"]["data"]["delta"]["text"] == "阶段一已完成"
    assert all(event["params"]["timestamp"] > 1_000_000_000_000 for event in events)


def test_parallel_child_chunks_keep_namespace_and_finish_independently() -> None:
    """一个子图结束只能关闭自己的消息流，不截断仍在输出的兄弟任务。"""
    projector = AgentProtocolEventProjector()
    for name in ("a", "b"):
        events = projector.project_many(AgentEvent(
            channel="messages", namespace=(name,), data=AIMessageChunk(content=name),
        ))
        assert all(event["params"]["namespace"] == [name] for event in events)
    finished = projector.project_many(AgentEvent(
        channel="lifecycle", namespace=("a",), data={"event": "completed"},
    ))
    assert all(event["params"]["namespace"] == ["a"] for event in finished)
    pending = projector.finish_pending_messages()
    assert all(event["params"]["namespace"] == ["b"] for event in pending)
    assert pending[0]["params"]["data"]["content"]["text"] == "b"


def test_progress_is_committed_and_published_before_slow_tool_executes(monkeypatch) -> None:
    """冻结批次计时，确保小批次内容和工具开始事件无需下一次回调即可到达订阅者。"""
    committed = []
    notifications = []
    results = []
    run_service = Mock()
    run_service.record_events.side_effect = lambda _run, events: committed.extend(events)
    queue = Mock()
    queue.notify_events.side_effect = lambda: notifications.append(len(committed))
    run = WorkbenchRun(run_id=uuid4(), conversation_id=uuid4(), spec=SimpleNamespace(
        account_id=uuid4(), app_id=uuid4(), conversation_id=uuid4(), message_id=uuid4(),
        input_message_id="input", invoke_from=InvokeFrom.DEBUGGER,
    ))

    def execute(_input, *, on_event, cancel_requested):
        """在模拟慢工具真正执行前检查传输边界，断言失败会随结果传到测试。"""
        on_event(AgentEvent(channel="messages", data=AIMessageChunk(content="已完成第一部分")))
        on_event(AgentEvent(channel="tools", data={
            "event": "tool-started", "tool_call_id": "slow", "tool_name": "read_file",
            "input": {"file_path": "/workspace/report.md"},
        }))
        assert committed[-1].method == "tools"
        assert any(event.method == "messages" and event.params.data["event"] == "content-block-delta" for event in committed)
        assert notifications[-1] == len(committed)
        return AgentExecutionResult(status="completed", answer="完成")

    agent = Mock()
    agent.execute.side_effect = execute
    assembly = Mock()
    assembly.build_agent.return_value = agent, HumanMessage(content="开始")
    service = AgentWorkbenchRunExecutionService(
        db=Mock(), agent_assembly_service=assembly, run_service=run_service, queue_manager=queue,
    )
    monkeypatch.setattr("internal.service.agent_workbench_execution_service.time.monotonic", lambda: 1.0)
    monkeypatch.setattr(service, "_finish_run", lambda *args, **kwargs: results.append(kwargs["result"]))
    with Flask(__name__).app_context():
        service.execute(run)
    assert results[0].error is None
    assert results[0].status == "completed"
    queue.finish.assert_called_once_with(run)


def test_tool_boundary_closes_phase_without_provider_finish_marker() -> None:
    """供应商遗漏终止标记时，后续回答仍应成为新阶段，子图输出继续保留。"""
    projector = AgentProtocolEventProjector(root_message_id="answer")
    projector.project_many(AgentEvent(channel="messages", data=AIMessageChunk(content="先检查")))
    projector.project_many(AgentEvent(channel="messages", namespace=("child",), data=AIMessageChunk(content="子任务")))
    boundary = projector.project_many(AgentEvent(channel="tools", data={
        "event": "tool-started", "tool_call_id": "call", "tool_name": "read_file", "input": {},
    }))
    assert [event["params"]["data"]["event"] for event in boundary] == [
        "content-block-finish", "message-finish", "tool-started",
    ]
    assert all(event["params"]["namespace"] == [] for event in boundary)
    next_phase = projector.project_many(AgentEvent(channel="messages", data=AIMessageChunk(content="检查完成")))
    assert next_phase[0]["params"]["data"]["event"] == "message-start"
    child_finish = projector.project_many(AgentEvent(channel="lifecycle", namespace=("child",), data={"event": "completed"}))
    assert child_finish[0]["params"]["data"]["content"]["text"] == "子任务"
