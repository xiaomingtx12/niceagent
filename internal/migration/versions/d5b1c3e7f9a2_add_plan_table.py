"""add plan table

Revision ID: d5b1c3e7f9a2
Revises: c4a0b2d6e8f1
Create Date: 2026-07-08 22:30:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d5b1c3e7f9a2"
down_revision = "c4a0b2d6e8f1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "plan",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("app_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "conversation_id", postgresql.UUID(as_uuid=False), nullable=False
        ),
        sa.Column("message_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'draft'::character varying"),
        ),
        sa.Column(
            "steps",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_plan_id"),
    )
    with op.batch_alter_table("plan", schema=None) as batch_op:
        batch_op.create_index(
            "plan_app_conversation_idx",
            ["app_id", "conversation_id"],
            unique=False,
        )
        batch_op.create_index(
            "plan_plan_id_version_idx",
            ["plan_id", "version"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("plan", schema=None) as batch_op:
        batch_op.drop_index("plan_plan_id_version_idx")
        batch_op.drop_index("plan_app_conversation_idx")
    op.drop_table("plan")
