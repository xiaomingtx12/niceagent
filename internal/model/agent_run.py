"""Persistent task runs and protocol events for the Agent workbench."""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UUID,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from internal.extension.database_extension import db


class AgentRun(db.Model):
    """One executable run inside a task conversation."""

    __tablename__ = "agent_run"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_agent_run_id"),
        Index("agent_run_conversation_created_idx", "conversation_id", "created_at"),
        Index("agent_run_scope_thread_idx", "scope_kind", "scope_value", "thread_id"),
    )

    id = Column(UUID, nullable=False, server_default=text("uuid_generate_v4()"))
    app_id = Column(UUID, nullable=False)
    conversation_id = Column(UUID, nullable=False)
    thread_id = Column(UUID, nullable=False)
    scope_kind = Column(String(32), nullable=False)
    scope_value = Column(String(255), nullable=False)
    invoke_from = Column(String(255), nullable=False, server_default=text("''"))
    created_by = Column(UUID, nullable=True)
    message_id = Column(UUID, nullable=True)
    status = Column(String(32), nullable=False, server_default=text("'queued'"))
    query = Column(Text, nullable=False, server_default=text("''"))
    values = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    last_sequence = Column(Integer, nullable=False, server_default=text("0"))
    error = Column(Text, nullable=False, server_default=text("''"))
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP(0)"))
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(0)"),
        server_onupdate=text("CURRENT_TIMESTAMP(0)"),
    )


class AgentRunEvent(db.Model):
    """Append-only projected Agent Protocol event."""

    __tablename__ = "agent_run_event"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_agent_run_event_id"),
        UniqueConstraint("run_id", "seq", name="agent_run_event_run_seq_uq"),
        UniqueConstraint("run_id", "event_id", name="agent_run_event_run_event_uq"),
        Index("agent_run_event_run_seq_idx", "run_id", "seq"),
    )

    id = Column(UUID, nullable=False, server_default=text("uuid_generate_v4()"))
    run_id = Column(UUID, nullable=False)
    seq = Column(Integer, nullable=False)
    event_id = Column(String(255), nullable=False)
    method = Column(String(64), nullable=False)
    namespace = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP(0)"))


__all__ = ["AgentRun", "AgentRunEvent"]
