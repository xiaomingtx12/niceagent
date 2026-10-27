"""Remove the obsolete custom long-term memory table.

Revision ID: d_v2_009_remove_memory_facts
Revises: d_v2_008_agent_trace_fields
Create Date: 2026-08-17

Long-term memory is now owned by DeepAgents file memory through the configured
LangGraph Store.  The old ``memory_facts`` table is not part of the Agent V2
runtime and must not remain as a second source of truth.
"""

from alembic import op
import sqlalchemy as sa


revision = "d_v2_009_remove_memory_facts"
down_revision = "d_v2_008_agent_trace_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Drop the obsolete application-managed memory facts table."""
    op.drop_table("memory_facts")


def downgrade() -> None:
    """Recreate an empty legacy table for migration rollback only."""
    op.create_table(
        "memory_facts",
        sa.Column(
            "id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=False),
        sa.Column("end_user_id", sa.UUID(), nullable=True),
        sa.Column("fact_type", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "importance",
            sa.Float(),
            nullable=False,
            server_default=sa.text("1.0"),
        ),
        sa.Column("source_conversation_id", sa.UUID(), nullable=True),
        sa.Column("source_message_id", sa.UUID(), nullable=True),
        sa.Column(
            "source_type",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'auto'"),
        ),
        sa.Column("auto_extracted", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(0)"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(0)"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memory_facts"),
        sa.CheckConstraint(
            "importance >= 0.0 AND importance <= 1.0",
            name="ck_memory_facts_confidence_range",
        ),
        sa.CheckConstraint(
            "source_type IN ('auto', 'manual', 'edited')",
            name="ck_memory_facts_source_type",
        ),
    )
    op.create_index(
        "idx_memory_facts_end_user",
        "memory_facts",
        ["account_id", "app_id", "end_user_id"],
    )
    op.create_index(
        "idx_memory_facts_app_updated",
        "memory_facts",
        ["account_id", "app_id", "updated_at"],
    )
    op.create_index(
        "idx_memory_facts_recall",
        "memory_facts",
        ["account_id", "app_id", "importance", "updated_at"],
    )
    op.create_index(
        "idx_memory_facts_deleted_at",
        "memory_facts",
        ["deleted_at"],
    )
