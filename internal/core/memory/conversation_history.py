"""把持久化会话原样加载为 LangChain messages。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from internal.entity.conversation_entity import MessageStatus
from internal.model import Conversation, Message
from pkg.sqlalchemy import SQLAlchemy


def load_history_messages(
    *,
    db: SQLAlchemy,
    conversation: Conversation | None,
) -> list[AnyMessage]:
    """加载完整历史，不在调用前裁剪或摘要。

    上下文预算、摘要触发、溢出重试和历史 offload 均交给 DeepAgents 默认的
    SummarizationMiddleware，以免业务层与框架各维护一套截断策略。
    """
    if conversation is None:
        return []

    persisted_messages = (
        db.session.query(Message)
        .filter(
            Message.conversation_id == conversation.id,
            Message.answer != "",
            Message.is_deleted == False,  # noqa: E712
            Message.status == MessageStatus.NORMAL,
        )
        .order_by(Message.created_at.asc())
        .all()
    )

    history: list[AnyMessage] = []
    for message in persisted_messages:
        history.extend(
            [
                HumanMessage(content=message.query),
                AIMessage(content=message.answer),
            ]
        )
    return history
