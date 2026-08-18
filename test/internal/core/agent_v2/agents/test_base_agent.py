"""BaseAgent 核心流程单测。

约束（硬约束 #5）：单测用 FakeChatModel + monkeypatch 跳过 Redis 注入，
不依赖真实数据库。

测试策略：
- BaseAgent(stream/invoke) 完整跑 deepagent stream 需要 LLM 支持 bind_tools +
  ChatResult 完整流程，超出单测能力（langchain_anthropic 等中间件 import 链
  在 hang）。完整 deepagent 流程覆盖在 test_factory.py + test_review_middleware.py。
- 本文件覆盖 BaseAgent 构造、调用、审核和线程配置契约。
"""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from pydantic import Field

from internal.core.agent_v2.agents.base_agent import BaseAgent
from internal.core.agent_v2.entities.agent_entity import AgentConfig, AgentState
from internal.core.agent_v2.entities.queue_entity import QueueEvent
from internal.entity.conversation_entity import InvokeFrom


class _BindToolsFakeChatModel(BaseChatModel):
    """Fake 模型：bind_tools 返回自己，_generate 返回 AIMessage。"""

    response_text: str = "fake"
    call_count: int = Field(default=0)

    @property
    def _llm_type(self) -> str:
        return "bind-tools-fake"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(self, messages, stop=None, **kwargs):
        self.call_count += 1
        from langchain_core.outputs import ChatGeneration, ChatResult
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.response_text))]
        )


class _MockRedis:
    """AgentQueueManager 注入用，提供 setex/get 桩（单测不关心内容）。"""

    def setex(self, *_a, **_k):
        return None

    def get(self, *_a, **_k):
        return None

    def delete(self, *_a, **_k):
        return None


@pytest.fixture(autouse=True)
def patch_injector(monkeypatch):
    """跳过 app.http.module.injector.get(Redis) 的 DI 链。"""
    from app.http.module import injector

    mock_redis = _MockRedis()
    monkeypatch.setattr(injector, "get", lambda *_args, **_kwargs: mock_redis)


def _config() -> AgentConfig:
    return AgentConfig(
        user_id=uuid4(),
        invoke_from=InvokeFrom.DEBUGGER,
        preset_prompt="test",
    )


def _llm(text: str = "hello from fake model"):
    return _BindToolsFakeChatModel(response_text=text)


def test_base_agent_invokes_build_deep_agent_in_init():
    """构造时 _agent 已是 CompiledStateGraph。"""
    from langgraph.graph.state import CompiledStateGraph

    agent = BaseAgent(llm=_llm(), agent_config=_config())
    assert isinstance(agent._agent, CompiledStateGraph)


def test_constructor_closes_runtime_backends_when_graph_build_fails(monkeypatch):
    """构图失败时立即释放本次运行的 Backend。"""
    import internal.core.agent_v2.agents.base_agent as base_agent_module

    backend = SimpleNamespace(close_calls=0)

    def close():
        """记录资源释放次数。"""
        backend.close_calls += 1

    backend.close = close
    composite = SimpleNamespace(default=backend, routes={"/workspace/": backend})
    monkeypatch.setattr(
        base_agent_module,
        "build_deep_agent",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("构图失败")),
    )

    with pytest.raises(RuntimeError, match="构图失败"):
        BaseAgent(
            llm=_llm(),
            agent_config=_config(),
            backend=composite,
        )

    assert backend.close_calls == 1


def test_base_agent_no_long_term_memory_in_agent_state():
    """R0 回归：AgentState 不再含 long_term_memory 字段。"""
    assert "long_term_memory" not in AgentState.__annotations__


def test_base_agent_agent_config_removed_long_term_memory():
    """R0 回归：AgentConfig 不再含 enable_long_term_memory 字段。"""
    assert "enable_long_term_memory" not in AgentConfig.model_fields


def test_base_agent_run_stream_in_thread_signature():
    """_run_stream_in_thread 接受 input/config/kwargs，方法存在可调用。"""
    agent = BaseAgent(llm=_llm(), agent_config=_config())
    assert hasattr(agent, "_run_stream_in_thread")
    assert callable(agent._run_stream_in_thread)


def test_merge_thread_config_keeps_tenant_identity_stable():
    config = _config().model_copy(
        update={
            "account_id": uuid4(),
            "app_id": uuid4(),
            "conversation_id": uuid4(),
        }
    )
    agent = BaseAgent.model_construct(llm=_llm(), agent_config=config)

    merged = agent._merge_thread_config(
        {
            "tags": ["chat"],
            "metadata": {
                "request_id": "req-1",
                "conversation_id": "forged",
            },
            "configurable": {
                "checkpoint_ns": "custom",
                "thread_id": "forged",
            },
        }
    )

    assert merged["configurable"]["thread_id"].endswith(",main)")
    assert merged["configurable"]["thread_id"] != "forged"
    assert merged["configurable"]["checkpoint_ns"] == "custom"
    assert merged["metadata"]["request_id"] == "req-1"
    assert merged["metadata"]["conversation_id"] == str(config.conversation_id)
    assert merged["tags"] == ["chat"]


def test_invoke_uses_latest_human_message_as_query(monkeypatch):
    agent = BaseAgent.model_construct(llm=_llm(), agent_config=_config())
    monkeypatch.setattr(BaseAgent, "stream", lambda *_args, **_kwargs: iter(()))

    result = agent.invoke(
        {
            "messages": [
                HumanMessage(content="旧问题"),
                AIMessage(content="旧回答"),
                HumanMessage(content="当前问题"),
            ]
        }
    )

    assert result.query == "当前问题"


def test_run_stream_uses_event_middleware_and_adds_single_terminal_event():
    task_id = uuid4()

    class FakeQueueManager:
        def __init__(self):
            self.events = []

        def publish(self, _task_id, thought):
            self.events.append(thought)

        def is_stopped(self, _task_id):
            return False

    agent = BaseAgent(llm=_llm("完成"), agent_config=_config())
    agent._agent_queue_manager = FakeQueueManager()

    agent._run_stream_in_thread(
        {"messages": [HumanMessage(content="问题")], "task_id": task_id},
        None,
    )

    assert [event.event for event in agent._agent_queue_manager.events] == [
        QueueEvent.AGENT_MESSAGE,
        QueueEvent.AGENT_END,
    ]
    assert agent._agent_queue_manager.events[0].answer == "完成"


def test_output_review_blocks_message_before_event_publication():
    task_id = uuid4()

    class FakeQueueManager:
        def __init__(self):
            self.events = []

        def publish(self, _task_id, thought):
            self.events.append(thought)

        def is_stopped(self, _task_id):
            return False

    config = _config().model_copy(
        update={
            "review_config": {
                "enable": True,
                "keywords": ["敏感内容"],
                "inputs_config": {"enable": False},
                "outputs_config": {"enable": True},
            }
        }
    )
    agent = BaseAgent(llm=_llm("这里有敏感内容"), agent_config=config)
    agent._agent_queue_manager = FakeQueueManager()

    agent._run_stream_in_thread(
        {"messages": [HumanMessage(content="问题")], "task_id": task_id},
        None,
    )

    assert [event.event for event in agent._agent_queue_manager.events] == [
        QueueEvent.ERROR,
    ]


def test_input_review_ends_graph_without_calling_model():
    """输入命中审核词时直接返回预设响应。"""
    task_id = uuid4()

    class FakeQueueManager:
        def __init__(self):
            self.events = []

        def publish(self, _task_id, thought):
            self.events.append(thought)

        def is_stopped(self, _task_id):
            return False

    config = _config().model_copy(
        update={
            "review_config": {
                "enable": True,
                "keywords": ["敏感输入"],
                "inputs_config": {
                    "enable": True,
                    "preset_response": "请求未通过审核",
                },
                "outputs_config": {"enable": False},
            }
        }
    )
    model = _llm("不应生成")
    agent = BaseAgent(llm=model, agent_config=config)
    agent._agent_queue_manager = FakeQueueManager()

    agent._run_stream_in_thread(
        {"messages": [HumanMessage(content="包含敏感输入")], "task_id": task_id},
        None,
    )

    assert model.call_count == 0
    assert [event.event for event in agent._agent_queue_manager.events] == [
        QueueEvent.AGENT_MESSAGE,
        QueueEvent.AGENT_END,
    ]
    assert agent._agent_queue_manager.events[0].answer == "请求未通过审核"
