"""Agent V2 durable trace model tests."""

from internal.model import MessageAgentThought


def test_message_agent_thought_has_p2_trace_columns():
    """The existing trace table owns delegation and Todo persistence fields."""
    columns = MessageAgentThought.__table__.columns

    assert {
        "agent_id",
        "agent_type",
        "parent_agent_id",
        "delegation_id",
        "task_id",
        "todos",
    }.issubset(columns.keys())
    assert columns.agent_id.type.length == 64
    assert columns.agent_type.type.length == 16
    assert columns.delegation_id.type.length == 128
    assert columns.task_id.nullable is True
    assert columns.todos.nullable is False


def test_message_agent_thought_has_trace_lookup_indexes():
    """Conversation trace lookups use delegation and Agent indexes."""
    indexes = {index.name for index in MessageAgentThought.__table__.indexes}

    assert "message_agent_thought_conversation_delegation_idx" in indexes
    assert "message_agent_thought_conversation_agent_created_idx" in indexes

