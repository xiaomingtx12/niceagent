"""agent_entity R0 改造单测。

覆盖：
- AgentConfig 不再有 enable_long_term_memory 字段
- AgentState 不再有 long_term_memory 字段
- QueueEvent 不再有 LONG_TERM_MEMORY_RECALL 枚举值
- AGENT_SYSTEM_PROMPT_TEMPLATE / REACT_AGENT_SYSTEM_PROMPT_TEMPLATE 不含
  <长期记忆> 占位
"""
from internal.core.agent.entities.agent_entity import (
    AGENT_SYSTEM_PROMPT_TEMPLATE,
    AgentConfig,
    AgentState,
)
from internal.core.agent.entities.queue_entity import QueueEvent


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


def test_agent_system_prompt_template_no_long_term_memory_section():
    """AGENT_SYSTEM_PROMPT_TEMPLATE 不含 <长期记忆> 段。"""
    assert "<长期记忆>" not in AGENT_SYSTEM_PROMPT_TEMPLATE
    assert "{long_term_memory}" not in AGENT_SYSTEM_PROMPT_TEMPLATE


def test_react_system_prompt_template_no_long_term_memory_section():
    """REACT_AGENT_SYSTEM_PROMPT_TEMPLATE 也不含 <长期记忆> 段。"""
    from internal.core.agent.entities.agent_entity import (
        REACT_AGENT_SYSTEM_PROMPT_TEMPLATE,
    )
    assert "<长期记忆>" not in REACT_AGENT_SYSTEM_PROMPT_TEMPLATE
    assert "{long_term_memory}" not in REACT_AGENT_SYSTEM_PROMPT_TEMPLATE