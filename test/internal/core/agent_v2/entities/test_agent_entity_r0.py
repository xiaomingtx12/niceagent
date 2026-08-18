"""agent_entity R0 改造单测。

覆盖：
- AgentConfig 不再有 enable_long_term_memory 字段
- AgentState 不再有 long_term_memory 字段
- QueueEvent 不再有 LONG_TERM_MEMORY_RECALL 枚举值
- 应用提示词不再包含旧长期记忆占位
"""
from internal.core.agent_v2.entities.agent_entity import (
    AgentConfig,
    AgentState,
)
from internal.core.agent_v2.entities.queue_entity import QueueEvent
from internal.core.agent_v2.context.prompt import BASE_AGENT_PROMPT


def test_agent_config_has_no_enable_long_term_memory():
    """R0 删除 enable_long_term_memory。"""
    assert "enable_long_term_memory" not in AgentConfig.model_fields


def test_agent_state_has_no_long_term_memory():
    """R0 删除 AgentState.long_term_memory。"""
    annotations = AgentState.__annotations__
    assert "long_term_memory" not in annotations


def test_queue_event_no_long_term_memory_recall():
    """R0 删除 QueueEvent.LONG_TERM_MEMORY_RECALL。"""
    assert not hasattr(QueueEvent, "LONG_TERM_MEMORY_RECALL")


def test_agent_system_prompt_has_no_long_term_memory_section():
    """应用提示词不再包含旧长期记忆段。"""
    assert "<长期记忆>" not in BASE_AGENT_PROMPT
    assert "{long_term_memory}" not in BASE_AGENT_PROMPT
