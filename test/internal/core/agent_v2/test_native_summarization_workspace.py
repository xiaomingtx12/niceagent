"""Native summarization offloads full history into the DB workspace."""

from uuid import uuid4

from deepagents.middleware.summarization import SummarizationMiddleware
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from internal.core.agent_v2.workspace_backend import MyDifyWorkspaceBackend
from internal.model import WorkspaceFile


def test_native_summarization_writes_conversation_history_file():
    import uuid as uuid_module

    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _register(dbapi_conn, _record):
        dbapi_conn.create_function(
            "uuid_generate_v4",
            0,
            lambda: str(uuid_module.uuid4()),
        )

    WorkspaceFile.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    account_id, app_id, conversation_id = uuid4(), uuid4(), uuid4()
    backend = MyDifyWorkspaceBackend(
        session=session,
        account_id=account_id,
        app_id=app_id,
        conversation_id=conversation_id,
    )
    model = FakeListChatModel(responses=["compressed context", "final answer"])
    middleware = SummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=("messages", 4),
        keep=("messages", 2),
        truncate_args_settings=None,
    )
    graph = create_agent(model=model, tools=[], middleware=[middleware])

    graph.invoke(
        {
            "messages": [
                HumanMessage(content="u1"),
                AIMessage(content="a1"),
                HumanMessage(content="u2"),
                AIMessage(content="a2"),
                HumanMessage(content="u3"),
            ]
        },
        config={"configurable": {"thread_id": str(conversation_id)}},
    )

    row = session.query(WorkspaceFile).one()
    assert row.account_id == account_id
    assert row.app_id == app_id
    assert row.conversation_id == conversation_id
    assert row.path == (
        f"{conversation_id}/conversation_history/{conversation_id}.md"
    )
    history = row.content.decode("utf-8")
    assert "<message type=\"human\">u1</message>" in history
    assert "<message type=\"ai\">a1</message>" in history
    assert backend.read(
        f"/conversation_history/{conversation_id}.md"
    ).file_data["content"] == history

    session.close()
    engine.dispose()
