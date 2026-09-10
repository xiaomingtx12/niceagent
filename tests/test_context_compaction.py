"""通过官方回调验证摘要观测、原生压缩和公开事件隔离。"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from deepagents import create_deep_agent
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel, FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import LLMResult, ChatGeneration

from internal.core.agent_v2.middleware.context_window_middleware import ContextWindowMiddleware, build_context_snapshot
from internal.core.agent_v2.entity.agent_event import AgentEvent
from internal.core.agent_v2.observability.run_observer import PlatformAgentRunObserver
from internal.service.agent_protocol.event_projector import AgentProtocolEventProjector


class Model(FakeListChatModel):
    """本地模型沿真实 SDK 工具绑定和摘要调用路径运行。"""

    def bind_tools(self, tools, **kwargs):
        """仅替代外部模型，不改动图及摘要中间件。"""
        return self


def make_request(model=None):
    """长历史触发 SDK 默认压缩阈值，不依赖私有中间件方法。"""
    model = model or Model(responses=["PRIVATE_SUMMARY", "Public answer"], profile={"max_input_tokens": 512})
    messages = [item for i in range(14) for item in (
        HumanMessage(content=f"Question {i}: " + "private history " * 30), AIMessage(content="Answer " * 30),
    )]
    return ModelRequest(model=model, messages=messages, state={"messages": messages}), []


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_graph_reports_summary_generation_without_claiming_compaction(asynchronous):
    """真实同步和异步 v3 流保留摘要事件，摘要正文不会成为公开回答。"""
    request, _ = make_request()
    observer = PlatformAgentRunObserver(uuid4())
    graph = create_deep_agent(model=request.model, middleware=[ContextWindowMiddleware()])
    config = {"callbacks": [observer]}

    async def collect():
        """异步执行也必须继承同一回调及图 writer。"""
        stream = await graph.astream_events({"messages": request.messages}, config=config, version="v3")
        return [raw async for raw in stream]

    raw_events = asyncio.run(collect()) if asynchronous else list(graph.stream_events(
        {"messages": request.messages}, config=config, version="v3"))
    projector = AgentProtocolEventProjector()
    public = [visible for raw in raw_events for event in projector.project_many(AgentEvent.from_raw(raw))
              if (visible := projector.to_public_event(event)) is not None]
    summaries = [event.params.data["payload"] for event in public
                 if event.method == "custom" and event.params.data["name"] == "context_compaction"]
    assert [item["phase"] for item in summaries] == ["summarizing", "completed"]
    assert len({item["compaction_id"] for item in summaries}) == 1
    for item in summaries:
        assert item["scope"] == "summary_generation"
        assert all(item[key] is None for key in ("before_tokens", "after_tokens", "reason", "history_saved", "summarized_messages"))
        assert projector.to_public_event(projector.generated("custom", {**item, "summary": "PRIVATE_SUMMARY"})).params.data["payload"] == item
    assert "PRIVATE_SUMMARY" not in str([event.model_dump() for event in public if event.method == "messages"])
    assert "private history" not in str(summaries)
    assert "Public answer" in str([event.model_dump() for event in public if event.method == "messages"])
    spans = observer.snapshot()["spans"]
    assert len([span for span in spans if span["attributes"].get("lc_source") == "summarization"]) == 1
    assert observer.build_run_usage()["model_call_count"] == 2


@pytest.mark.parametrize("error,phase", [(RuntimeError("PRIVATE_ERROR"), "failed"), (asyncio.CancelledError(), "cancelled")])
def test_callback_failure_and_cancellation_are_private_and_terminal(monkeypatch, error, phase):
    """结束回调使用开始时的 writer，重复结束或迟到结束不会重复发布。"""
    events = []
    monkeypatch.setattr("internal.core.agent_v2.observability.summary_progress.get_stream_writer", lambda: events.append)
    observer = PlatformAgentRunObserver(uuid4())
    call_id = uuid4()
    observer.on_chat_model_start({}, [[HumanMessage(content="PRIVATE_PROMPT")]], run_id=call_id, metadata={"lc_source": "summarization"})
    observer.on_llm_error(error, run_id=call_id)
    observer.on_llm_error(error, run_id=call_id)
    assert [item["phase"] for item in events] == ["summarizing", phase]
    assert "PRIVATE" not in str(events)


def test_concurrent_summary_calls_keep_writers_and_usage_separate(monkeypatch):
    """交错的子图调用按模型调用 ID 区分，统计一次且保留供应商零用量。"""
    first, second = [], []
    observer = PlatformAgentRunObserver(uuid4())
    one, two = uuid4(), uuid4()
    for call_id, target in ((one, first), (two, second)):
        monkeypatch.setattr("internal.core.agent_v2.observability.summary_progress.get_stream_writer", lambda: target.append)
        observer.on_chat_model_start({}, [], run_id=call_id, metadata={"lc_source": "summarization"})
    response = LLMResult(generations=[[ChatGeneration(message=AIMessage(content="PRIVATE_SUMMARY", usage_metadata={
        "input_tokens": 0, "output_tokens": 12, "total_tokens": 12,
    }))]])
    observer.on_llm_end(response, run_id=two)
    observer.on_llm_end(response, run_id=one)
    for call_id, events in ((one, first), (two, second)):
        assert [item["phase"] for item in events] == ["summarizing", "completed"]
        assert {item["compaction_id"] for item in events} == {str(call_id)}
        assert events[-1]["input_tokens"] == 0
        assert events[-1]["output_tokens"] == 12
    assert observer.build_run_usage()["output_tokens"] == 24


def test_ordinary_calls_and_missing_graph_context_do_not_emit_summary(monkeypatch):
    """普通调用不伪造摘要事件，无图上下文时保留模型观测。"""
    events = []
    monkeypatch.setattr("internal.core.agent_v2.observability.summary_progress.get_stream_writer", lambda: events.append)
    observer = PlatformAgentRunObserver(uuid4())
    observer.on_chat_model_start({}, [], run_id=uuid4())
    assert not events

    def no_context():
        """独立 invoke 无法取得图事件 writer。"""
        raise RuntimeError("no graph")

    monkeypatch.setattr("internal.core.agent_v2.observability.summary_progress.get_stream_writer", no_context)
    observer.on_chat_model_start({}, [], run_id=uuid4(), metadata={"lc_source": "summarization"})
    assert observer.build_run_usage()["model_call_count"] == 2
    assert not events


def test_native_subagent_summary_keeps_child_namespace():
    """主图回调传播到同步子图，摘要事件不会被误归到主 Agent。"""
    class ParentModel(FakeMessagesListChatModel):
        """确定性工具调用通过 SDK 原生 task 进入子 Agent。"""

        def bind_tools(self, tools, **kwargs):
            """保留内置工具执行，只替代模型请求。"""
            return self

    parent = ParentModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "task", "id": "child-task", "args": {
            "subagent_type": "helper", "description": "Research " + "private history " * 2000,
        }}]),
        AIMessage(content="Public parent answer"),
    ])
    child = ParentModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "ls", "id": "child-ls", "args": {"path": "/"}}]),
        AIMessage(content="PRIVATE_CHILD_SUMMARY"),
        AIMessage(content="Child answer"),
    ], profile={"max_input_tokens": 4096})
    observer = PlatformAgentRunObserver(uuid4())
    graph = create_deep_agent(model=parent, middleware=[ContextWindowMiddleware()], subagents=[{
        "name": "helper", "description": "Research helper", "system_prompt": "Research",
        "model": child, "tools": [],
    }])
    projector = AgentProtocolEventProjector()
    public = []
    for raw in graph.stream_events({"messages": [HumanMessage(content="Delegate research")]},
                                   config={"callbacks": [observer]}, version="v3"):
        for event in projector.project_many(AgentEvent.from_raw(raw)):
            visible = projector.to_public_event(event)
            if visible is not None:
                public.append(visible)
    summaries = [event for event in public if event.method == "custom" and event.params.data["name"] == "context_compaction"]
    assert [event.params.data["payload"]["phase"] for event in summaries] == ["summarizing", "completed"]
    assert all(event.params.namespace for event in summaries)
    assert summaries[0].params.namespace == summaries[1].params.namespace
    assert "PRIVATE_CHILD_SUMMARY" not in str([event.model_dump() for event in public if event.method == "messages"])


def test_closed_observation_channel_does_not_break_model_accounting(monkeypatch):
    """回调通知失败不能跳过 Span 结束或使模型调用失败。"""
    def closed(_):
        """模拟图事件消费者已经关闭。"""
        raise RuntimeError("closed stream")

    monkeypatch.setattr("internal.core.agent_v2.observability.summary_progress.get_stream_writer", lambda: closed)
    observer = PlatformAgentRunObserver(uuid4())
    call_id = uuid4()
    observer.on_chat_model_start({}, [], run_id=call_id, metadata={"lc_source": "summarization"})
    observer.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content="Summary"))]]), run_id=call_id)
    assert observer.snapshot()["spans"][0]["status"] == "ok"
    assert observer.build_run_usage()["model_call_count"] == 1


def test_compact_history_query_is_bounded_and_preserves_context_events(monkeypatch):
    """Exercise the PostgreSQL query and public projection without a live database."""
    from sqlalchemy.orm import Session, Query
    from sqlalchemy.dialects import postgresql
    from internal.service.agent_workbench_run_service import AgentWorkbenchRunService
    from internal.service.agent_protocol.state_projector import project_run_history

    run_id = uuid4()
    snapshot = build_context_snapshot(make_request()[0])
    projector = AgentProtocolEventProjector(run_id)
    payload = projector.generated("custom", snapshot)
    queries = []

    def rows(query):
        """Inspect the real ORM query while supplying known persisted event rows."""
        queries.append(str(query.statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})))
        return [SimpleNamespace(run_id=run_id, payload=payload)]

    monkeypatch.setattr(Query, "all", rows)
    with Session() as session:
        service = AgentWorkbenchRunService(db=SimpleNamespace(session=session))
        restored = service.list_context_events_by_run([run_id])
    assert "row_number() OVER (PARTITION BY agent_run_event.run_id" in queries[0]
    assert "position <= 64" in queries[0]
    assert "'custom'" in queries[0]
    assert restored[run_id][0].params.data["payload"]["call_id"] == snapshot["call_id"]
    run = SimpleNamespace(id=run_id, status="completed", last_event_sequence=10,
                          state_snapshot={}, message_id=uuid4(), input_message_id=None)
    history = project_run_history([run], restored)
    assert history[0].method == "custom"
    assert history[-1].method == "lifecycle"
