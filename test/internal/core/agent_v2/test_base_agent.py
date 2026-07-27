"""BaseAgent 核心流程单测。

约束（硬约束 #5）：单测用 FakeChatModel + monkeypatch 跳过 Redis 注入，
不依赖真实数据库。

测试策略：
- BaseAgent(stream/invoke) 完整跑 deepagent stream 需要 LLM 支持 bind_tools +
  ChatResult 完整流程，超出单测能力（langchain_anthropic 等中间件 import 链
  在 hang）。完整 deepagent 流程覆盖在 test_factory.py + test_review_middleware.py。
- 本文件覆盖 BaseAgent 内部逻辑：构造、queue_manager 暴露、AgentConfig 字段、
  AgentState 字段回归。
"""
from uuid import uuid4

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from internal.core.agent.agents.base_agent import BaseAgent
from internal.core.agent.entities.agent_entity import AgentConfig, AgentState
from internal.entity.conversation_entity import InvokeFrom


class _BindToolsFakeChatModel(BaseChatModel):
    """Fake 模型：bind_tools 返回自己，_generate 返回 AIMessage。"""

    response_text: str = "fake"

    @property
    def _llm_type(self) -> str:
        return "bind-tools-fake"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(self, messages, stop=None, **kwargs):
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


def test_base_agent_exposes_queue_manager():
    """BaseAgent.queue_manager property 暴露 AgentQueueManager。"""
    agent = BaseAgent(llm=_llm(), agent_config=_config())
    assert agent.queue_manager is not None
    assert agent.queue_manager.user_id == agent.agent_config.user_id
    assert agent.queue_manager.invoke_from == InvokeFrom.DEBUGGER


def test_base_agent_invokes_build_deep_agent_in_init():
    """构造时 _agent 已是 CompiledStateGraph。"""
    from langgraph.graph.state import CompiledStateGraph

    agent = BaseAgent(llm=_llm(), agent_config=_config())
    assert isinstance(agent._agent, CompiledStateGraph)


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


def test_base_agent_publish_call_does_not_raise_with_mock_redis():
    """queue_manager.publish 用 MockRedis 不抛错（验证 mock 桩足够）。"""
    from uuid import uuid4 as _u

    agent = BaseAgent(llm=_llm(), agent_config=_config())
    # 直接构造 AgentThought 推给 queue；不应抛 Redis 相关异常
    from internal.core.agent.entities.queue_entity import AgentThought, QueueEvent

    thought = AgentThought(
        id=_u(),
        task_id=_u(),
        event=QueueEvent.PING,
    )
    agent.queue_manager.publish(_u(), thought)  # 不抛错即可