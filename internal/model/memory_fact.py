"""Structured, user-manageable long-term memory facts."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UUID,
    text,
)
from sqlalchemy.orm import synonym

from internal.extension.database_extension import db


class MemoryFact(db.Model):
    """A discrete fact owned by an end user inside one application."""

    __tablename__ = "memory_facts"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_memory_facts"),
        Index("idx_memory_facts_end_user", "account_id", "app_id", "end_user_id"),
        Index("idx_memory_facts_app_updated", "account_id", "app_id", "updated_at"),
        Index(
            "idx_memory_facts_recall",
            "account_id",
            "app_id",
            "importance",
            "updated_at",
        ),
        Index("idx_memory_facts_deleted_at", "deleted_at"),
        CheckConstraint(
            "importance >= 0.0 AND importance <= 1.0",
            name="ck_memory_facts_confidence_range",
        ),
        CheckConstraint(
            "source_type IN ('auto', 'manual', 'edited')",
            name="ck_memory_facts_source_type",
        ),
    )

    id = Column(UUID, server_default=text("uuid_generate_v4()"), nullable=False)
    account_id = Column(UUID, nullable=False)
    app_id = Column(UUID, nullable=False)
    end_user_id = Column(UUID, nullable=True)
    # Retained for backward-compatible filtering; source_type is the ownership
    # provenance required by the final design.
    fact_type = Column(String(64), nullable=False, server_default=text("'general'"))
    content = Column(Text, nullable=False)
    importance = Column(Float, nullable=False, server_default=text("1.0"))
    confidence = synonym("importance")
    source_conversation_id = Column(UUID, nullable=True)
    source_message_id = Column(UUID, nullable=True)
    source_type = Column(String(16), nullable=False, server_default=text("'auto'"))
    auto_extracted = Column(Boolean, nullable=False, server_default=text("true"))
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "account_id": str(self.account_id),
            "app_id": str(self.app_id),
            "end_user_id": str(self.end_user_id) if self.end_user_id else None,
            "fact_type": self.fact_type,
            "content": self.content,
            "importance": self.importance,
            "confidence": self.importance,
            "source_conversation_id": (
                str(self.source_conversation_id)
                if self.source_conversation_id
                else None
            ),
            "source_message_id": (
                str(self.source_message_id) if self.source_message_id else None
            ),
            "source_type": self.source_type,
            "auto_extracted": self.auto_extracted,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
