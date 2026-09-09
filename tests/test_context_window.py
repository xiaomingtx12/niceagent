"""上下文快照的请求计数、公开边界和 v3 传输测试。"""

from types import SimpleNamespace
from uuid import uuid4

from deepagents import create_deep_agent
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.store.memory import InMemoryStore

from internal.core.agent_v2.entity.agent_event import AgentEvent
from internal.core.agent_v2.middleware.context_window_middleware import (
    ContextWindowMiddleware, build_context_snapshot, complete_context_snapshot,
)
from internal.service.agent_protocol.event_projector import AgentProtocolEventProjector
from internal.schema.agent_workbench_schema import WorkbenchEventStreamReq
from internal.core.agent_v2.entity.run_context import AgentRunContext


def request_with(**kwargs):
    """直接构造模型边界的请求，避免触发实际 provider。"""
    return ModelRequest(model=FakeListChatModel(responses=["ok"], metadata={"context_window": 8192}),
                        messages=[HumanMessage(content="私有用户问题")], **kwargs)


def test_snapshot_contains_only_counts_and_honors_output_reserve():
    """敏感提示词和文件正文不可随公开快照传输。"""
    request = request_with(system_message=SystemMessage(content="私有系统指令\n<长期记忆参考>用户偏好</长期记忆参考>"),
                           model_settings={"max_tokens": 1024})
    snapshot = build_context_snapshot(request)
    assert snapshot["context_limit"] == 8192
    assert snapshot["limit_kind"] == "context"
    assert snapshot["output_reserve"] == 1024
    assert snapshot["actual_input_tokens"] is None
    assert all(text not in str(snapshot) for text in ["私有用户问题", "私有系统指令", "用户偏好"])
    assert next(section for section in snapshot["sections"] if section["key"] == "memory")["tokens"] > 0
    assert sum(section["tokens"] for section in snapshot["sections"]) == snapshot["estimated_input_tokens"]


def test_actual_usage_is_per_call_and_zero_is_not_unknown():
    """供应商实际输入为零时保留零；缺失输入字段不能用输出 token 冒充。"""
    snapshot = build_context_snapshot(request_with())
    completed = complete_context_snapshot(snapshot, ModelResponse(result=[AIMessage(content="ok", usage_metadata={"input_tokens": 0, "output_tokens": 12, "total_tokens": 12})]))
    assert completed["actual_input_tokens"] == 0
    output_only = complete_context_snapshot(snapshot, ModelResponse(result=[AIMessage(content="ok", response_metadata={"usage": {"output_tokens": 12}})]))
    assert output_only["actual_input_tokens"] is None
    null_input = complete_context_snapshot(snapshot, ModelResponse(result=[AIMessage(content="ok", response_metadata={"usage": {"input_tokens": None, "output_tokens": 12}})]))
    assert null_input["actual_input_tokens"] is None


def test_unknown_capacity_and_multimodal_payload_are_not_fabricated():
    """未知模型不假设 128k；图片的 base64 不按文本字符计算。"""
    request = ModelRequest(model=FakeListChatModel(responses=["ok"]), messages=[HumanMessage(content=[
        {"type": "text", "text": "看这张图片"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100000}},
    ])])
    snapshot = build_context_snapshot(request)
    assert snapshot["context_limit"] is None
    assert snapshot["has_multimodal"] is True
    assert snapshot["estimated_input_tokens"] < 100


def test_public_projection_only_accepts_known_custom_schema():
    """开放上下文事件不等于开放所有框架自定义数据。"""
    projector = AgentProtocolEventProjector()
    assert projector.to_public_event(projector.generated("custom", {"type": "debug", "secret": "private"})) is None
    snapshot = {**build_context_snapshot(request_with()), "system_prompt": "private"}
    event = projector.to_public_event(projector.generated("custom", snapshot))
    assert event is not None
    assert "system_prompt" not in event.params.data["payload"]
    assert projector.to_public_event(event) == event
    assert WorkbenchEventStreamReq(channels=["custom"]).matches(event)


def test_real_v3_stream_emits_before_and_after_snapshots():
    """最终模型调用边界生成的 custom 事件可进入平台公开历史和 SSE。"""
    identity = uuid4()
    context = AgentRunContext(account_id=identity, app_id=uuid4(), conversation_id=uuid4(),
                              created_by=identity, principal_id=identity)
    # FakeListChatModel 的 bind_tools 不实现，测试子类只用于本地模型协议。
    class Model(FakeListChatModel):
        """本地无工具回答仍使用生产 DeepAgents 装配和 v3 事件通道。"""

        def bind_tools(self, tools, **kwargs):
            """不执行工具，不访问外部服务。"""
            return self

    agent = create_deep_agent(model=Model(responses=["测试回答"]), middleware=[ContextWindowMiddleware()], context_schema=AgentRunContext, store=InMemoryStore())
    projector = AgentProtocolEventProjector()
    snapshots = []
    for raw in agent.stream_events({"messages": [HumanMessage(content="你好")]}, context=context, version="v3"):
        for event in projector.project_many(AgentEvent.from_raw(raw)):
            public = projector.to_public_event(event)
            if public and public.method == "custom":
                snapshots.append(public.params.data["payload"])
    assert [snapshot["status"] for snapshot in snapshots] == ["running", "completed"]
    assert snapshots[0]["call_id"] == snapshots[1]["call_id"]
    assert snapshots[0]["sections"] == snapshots[1]["sections"]
