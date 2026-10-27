"""Add durable Agent V2 trace correlation fields.

Revision ID: d_v2_008_agent_trace_fields
Revises: d_v2_007_subagents_array
Create Date: 2026-07-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d_v2_008_agent_trace_fields"
down_revision = "d_v2_007_subagents_array"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add synchronous delegation metadata and native Todo snapshots."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"]
        for column in inspector.get_columns("message_agent_thought")
    }
    column_definitions = (
        ("agent_id", sa.Column("agent_id", sa.String(length=64), nullable=True)),
        ("agent_type", sa.Column("agent_type", sa.String(length=16), nullable=True)),
        (
            "parent_agent_id",
            sa.Column("parent_agent_id", sa.String(length=64), nullable=True),
        ),
        (
            "delegation_id",
            sa.Column("delegation_id", sa.String(length=128), nullable=True),
        ),
        ("task_id", sa.Column("task_id", sa.String(length=128), nullable=True)),
        (
            "todos",
            sa.Column(
                "todos",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'[]'::jsonb"),
                nullable=False,
            ),
        ),
    )
    for name, column in column_definitions:
        if name not in columns:
            op.add_column("message_agent_thought", column)
            columns.add(name)

    index_names = {
        index["name"] for index in inspector.get_indexes("message_agent_thought")
    }
    indexes = (
        (
            "message_agent_thought_conversation_delegation_idx",
            ["conversation_id", "delegation_id"],
        ),
        (
            "message_agent_thought_conversation_agent_created_idx",
            ["conversation_id", "agent_id", "created_at"],
        ),
    )
    for name, columns in indexes:
        if name not in index_names:
            op.create_index(
                name,
                "message_agent_thought",
                columns,
                unique=False,
            )


def downgrade() -> None:
    """Remove Agent V2 trace fields in reverse dependency order."""
    op.drop_index(
        "message_agent_thought_conversation_agent_created_idx",
        table_name="message_agent_thought",
    )
    op.drop_index(
        "message_agent_thought_conversation_delegation_idx",
        table_name="message_agent_thought",
    )
    op.drop_column("message_agent_thought", "todos")
    op.drop_column("message_agent_thought", "task_id")
    op.drop_column("message_agent_thought", "delegation_id")
    op.drop_column("message_agent_thought", "parent_agent_id")
    op.drop_column("message_agent_thought", "agent_type")
    op.drop_column("message_agent_thought", "agent_id")
