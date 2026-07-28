"""Final AgentConfig contract tests."""

from uuid import uuid4

from internal.core.agent.entities.agent_entity import AgentConfig
from internal.entity.conversation_entity import InvokeFrom


def test_agent_config_has_explicit_runtime_identity_fields():
    config = AgentConfig(
        user_id=uuid4(),
        invoke_from=InvokeFrom.DEBUGGER,
        account_id=uuid4(),
        app_id=uuid4(),
        conversation_id=uuid4(),
        end_user_id=uuid4(),
    )
    assert config.account_id is not None
    assert config.app_id is not None
    assert config.conversation_id is not None
    assert config.end_user_id is not None


def test_agent_config_uses_native_summarization_and_filesystem_defaults():
    assert "max_message_count" not in AgentConfig.model_fields
    assert "enable_workspace" not in AgentConfig.model_fields


def test_agent_config_subagents_default_to_final_list_shape():
    assert AgentConfig(user_id=uuid4()).subagents == []


def test_agent_config_normalizes_legacy_subagent_dictionary():
    config = AgentConfig(
        user_id=uuid4(),
        subagents={
            "researcher": {"system_prompt": "...", "tools": []},
        },
    )
    assert config.subagents == [
        {
            "name": "researcher",
            "system_prompt": "...",
            "tools": [],
        }
    ]


def test_agent_config_runtime_ids_remain_optional_for_isolated_unit_tests():
    config = AgentConfig(user_id=uuid4())
    assert str(config.account_id or "") == ""
    assert str(config.app_id or "") == ""
    assert str(config.conversation_id or "") == ""
