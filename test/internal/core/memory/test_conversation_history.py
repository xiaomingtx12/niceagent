from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

from internal.core.memory import conversation_history


class _Column:
    def __eq__(self, other):
        return ("eq", other)

    def __ne__(self, other):
        return ("ne", other)

    def asc(self):
        return "created_at_asc"


class _MessageModel:
    conversation_id = _Column()
    answer = _Column()
    is_deleted = _Column()
    status = _Column()
    created_at = _Column()


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.order = None

    def filter(self, *_conditions):
        return self

    def order_by(self, order):
        self.order = order
        return self

    def all(self):
        return self.rows


def test_load_history_messages_returns_all_rows_in_storage_order(monkeypatch):
    query = _Query(
        [
            SimpleNamespace(query="问题一", answer="回答一"),
            SimpleNamespace(query="问题二", answer="回答二"),
        ]
    )
    db = SimpleNamespace(
        session=SimpleNamespace(query=lambda model: query),
    )
    monkeypatch.setattr(conversation_history, "Message", _MessageModel)

    messages = conversation_history.load_history_messages(
        db=db,
        conversation=SimpleNamespace(id="conversation-1"),
    )

    assert query.order == "created_at_asc"
    assert [type(message) for message in messages] == [
        HumanMessage,
        AIMessage,
        HumanMessage,
        AIMessage,
    ]
    assert [message.content for message in messages] == [
        "问题一",
        "回答一",
        "问题二",
        "回答二",
    ]


def test_load_history_messages_handles_missing_conversation():
    assert conversation_history.load_history_messages(
        db=SimpleNamespace(),
        conversation=None,
    ) == []
