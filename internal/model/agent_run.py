"""Agent 工作台运行记录与持久化协议事件。"""

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UUID,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from internal.extension.database_extension import db


class AgentRun(db.Model):
    """一次工作台 Agent 执行的持久化摘要。"""

    __tablename__ = "agent_run"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_agent_run_id"),
        Index(
            "agent_run_conversation_source_created_idx",
            "conversation_id",
            "invoke_from",
            "created_at",
        ),
        Index(
            "agent_run_conversation_executing_uq",
            "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'cancelling')"),
        ),
        Index(
            "agent_run_conversation_input_message_uq",
            "conversation_id",
            "input_message_id",
            unique=True,
            postgresql_where=text("input_message_id IS NOT NULL"),
        ),
    )

    id = Column(UUID, nullable=False, server_default=text("uuid_generate_v4()"))
    app_id = Column(UUID, nullable=False)
    conversation_id = Column(UUID, nullable=False)
    invoke_from = Column(String(32), nullable=False)
    message_id = Column(UUID, nullable=False)
    input_message_id = Column(String(255), nullable=True)
    status = Column(String(32), nullable=False, server_default=text("'queued'"))
    state_snapshot = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    observability = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    last_event_sequence = Column(Integer, nullable=False, server_default=text("0"))
    error_message = Column(Text, nullable=False, server_default=text("''"))
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP(0)")
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(0)"),
        server_onupdate=text("CURRENT_TIMESTAMP(0)"),
    )


class AgentRunEvent(db.Model):
    """按运行序号追加的 Agent Protocol 投影事件。"""

    __tablename__ = "agent_run_event"
    __table_args__ = (
        PrimaryKeyConstraint(
            "run_id",
            "sequence",
            name="pk_agent_run_event_run_sequence",
        ),
    )

    run_id = Column(UUID, nullable=False)
    sequence = Column(Integer, nullable=False)
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP(0)")
    )


__all__ = ["AgentRun", "AgentRunEvent"]
