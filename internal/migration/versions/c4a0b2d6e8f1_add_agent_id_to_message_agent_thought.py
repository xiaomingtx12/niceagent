"""add agent_id to message_agent_thought

Revision ID: c4a0b2d6e8f1
Revises: b3f2a1c8d4e5
Create Date: 2026-07-08 21:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "c4a0b2d6e8f1"
down_revision = "b3f2a1c8d4e5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("message_agent_thought", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "agent_id",
                sa.String(length=255),
                nullable=False,
                server_default=sa.text("'supervisor'::character varying"),
                comment="事件来源Agent标识（多Agent协作时标记子Agent，默认supervisor）",
            )
        )


def downgrade():
    with op.batch_alter_table("message_agent_thought", schema=None) as batch_op:
        batch_op.drop_column("agent_id")
