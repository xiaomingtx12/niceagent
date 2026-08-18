"""P2 Agent trace persistence tests."""

from types import SimpleNamespace
from uuid import uuid4

from internal.core.agent_v2.entities.queue_entity import (
    AgentThought,
    AgentTodo,
    AgentType,
    QueueEvent,
)
from internal.entity.conversation_entity import InvokeFrom
from internal.model import Conversation, Message, MessageAgentThought
from internal.service.conversation_service import ConversationService


def test_save_agent_thoughts_persists_delegation_and_todo_events(monkeypatch):
    """P2 public events persist with stable trace correlation metadata."""
    service = ConversationService(
        db=SimpleNamespace(),
        provider_api_key_service=SimpleNamespace(),
    )
    conversation = SimpleNamespace(id=uuid4())
    message = SimpleNamespace(id=uuid4(), invoke_from=InvokeFrom.WEB_APP)
    created_rows: list[dict] = []

    def fake_get(model, _record_id):
        """Return the fixture matching the requested persistence model."""
        return conversation if model is Conversation else message

    def fake_create(model, **values):
        """Capture trace rows without opening a database transaction."""
        assert model is MessageAgentThought
        created_rows.append(values)
        return SimpleNamespace(**values)

    monkeypatch.setattr(service, "get", fake_get)
    monkeypatch.setattr(service, "create", fake_create)
    monkeypatch.setattr(service, "update", lambda *_args, **_kwargs: None)

    delegation_id = "call-researcher-1"
    request_task_id = uuid4()
    thoughts = [
        AgentThought(
            id=uuid4(),
            task_id=request_task_id,
            event=QueueEvent.SUB_AGENT_DELEGATE,
            tool="researcher",
            agent_id="main",
            delegation_id=delegation_id,
        ),
        AgentThought(
            id=uuid4(),
            task_id=request_task_id,
            event=QueueEvent.SUB_AGENT_END,
            tool="researcher",
            agent_id="researcher",
            agent_type=AgentType.SUBAGENT,
            parent_agent_id="main",
            delegation_id=delegation_id,
        ),
        AgentThought(
            id=uuid4(),
            task_id=request_task_id,
            event=QueueEvent.TODO_UPDATE,
            todos=[AgentTodo(content="核对证据", status="in_progress")],
        ),
    ]

    service.save_agent_thoughts(
        account_id=uuid4(),
        app_id=uuid4(),
        app_config={},
        conversation_id=conversation.id,
        message_id=message.id,
        agent_thoughts=thoughts,
    )

    assert [row["event"] for row in created_rows] == [
        QueueEvent.SUB_AGENT_DELEGATE,
        QueueEvent.SUB_AGENT_END,
        QueueEvent.TODO_UPDATE,
    ]
    assert created_rows[0]["delegation_id"] == delegation_id
    assert created_rows[1]["agent_type"] == "subagent"
    assert created_rows[1]["parent_agent_id"] == "main"
    assert created_rows[2]["todos"] == [
        {"content": "核对证据", "status": "in_progress"}
    ]
    assert all(row["task_id"] is None for row in created_rows)
    assert all(row["invoke_from"] == InvokeFrom.WEB_APP for row in created_rows)

