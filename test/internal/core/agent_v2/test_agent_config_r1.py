"""AgentConfig R1 新增字段单测。"""
from uuid import uuid4

from internal.core.agent.entities.agent_entity import AgentConfig
from internal.entity.conversation_entity import InvokeFrom


def test_agent_config_r1_has_three_id_fields():
    """R1: account_id / app_id / conversation_id 显式字段。"""
    cfg = AgentConfig(
        user_id=uuid4(),
        invoke_from=InvokeFrom.DEBUGGER,
        account_id=uuid4(),
        app_id=uuid4(),
        conversation_id=uuid4(),
    )
    assert cfg.account_id is not None
    assert cfg.app_id is not None
    assert cfg.conversation_id is not None


def test_agent_config_r1_max_message_count_default():
    """R1: max_message_count 默认 20 (deepagents SummarizationMiddleware 阈值)。"""
    cfg = AgentConfig(user_id=uuid4())
    assert cfg.max_message_count == 20


def test_agent_config_r1_enable_workspace_default_true():
    """R1: enable_workspace 默认 True (FilesystemMiddleware 默认挂上)。"""
    cfg = AgentConfig(user_id=uuid4())
    assert cfg.enable_workspace is True


def test_agent_config_r1_subagents_default_empty():
    """R1: subagents 默认空 dict (R3 启用, R1 不消费)。"""
    cfg = AgentConfig(user_id=uuid4())
    assert cfg.subagents == {}


def test_agent_config_r1_subagents_accepts_dict():
    """R1: subagents 接受 dict (R3 AsyncSubAgentMiddleware 消费)."""
    cfg = AgentConfig(
        user_id=uuid4(),
        subagents={
            "researcher": {"system_prompt": "...", "tools": []},
        },
    )
    assert "researcher" in cfg.subagents
    assert cfg.subagents["researcher"]["system_prompt"] == "..."


def test_agent_config_r1_optional_ids_accept_none():
    """R1: 三 ID 可缺省 (单测 / 默认 BaseAgent 用); str() 后为空串而不是 'None'。"""
    cfg = AgentConfig(user_id=uuid4())
    assert str(cfg.account_id or "") == ""
    assert str(cfg.app_id or "") == ""
    assert str(cfg.conversation_id or "") == ""