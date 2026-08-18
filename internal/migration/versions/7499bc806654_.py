"""legacy skill and memory schema adjustments

Revision ID: 7499bc806654
Revises: e7a2b1c3d4f5
Create Date: 2026-07-20 15:31:21.710434
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "7499bc806654"
down_revision = "e7a2b1c3d4f5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "memory_facts",
        sa.Column(
            "fact_id",
            sa.UUID(),
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("agent_name", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column(
            "content",
            sa.Text(),
            server_default=sa.text("''::text"),
            nullable=False,
        ),
        sa.Column(
            "confidence",
            sa.Float(),
            server_default=sa.text("1.0"),
            nullable=False,
        ),
        sa.Column("expected_valid_days", sa.Integer(), nullable=True),
        sa.Column(
            "staleness_protected",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "embedding",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.String(length=32),
            server_default=sa.text("'auto'::character varying"),
            nullable=False,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP(0)"),
            nullable=False,
        ),
        sa.Column("last_validated_at", sa.DateTime(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP(0)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("fact_id", name="pk_memory_facts_id"),
    )
    with op.batch_alter_table("memory_facts", schema=None) as batch_op:
        batch_op.create_index(
            "idx_memory_facts_category",
            ["user_id", "agent_name", "category", "is_active"],
            unique=False,
        )
        batch_op.create_index(
            "idx_memory_facts_thread_id",
            ["thread_id"],
            unique=False,
        )
        batch_op.create_index(
            "idx_memory_facts_user_agent_active",
            ["user_id", "agent_name", "is_active"],
            unique=False,
        )

    with op.batch_alter_table("app", schema=None) as batch_op:
        batch_op.alter_column(
            "app_type",
            existing_type=sa.VARCHAR(length=255),
            comment=None,
            existing_nullable=False,
            existing_server_default=sa.text("'chat'::character varying"),
        )

    for table in ("app_config", "app_config_version"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "enabled_skills",
                    postgresql.JSONB(astext_type=sa.Text()),
                    server_default=sa.text("'[]'::jsonb"),
                    nullable=False,
                )
            )


def downgrade():
    for table in ("app_config", "app_config_version"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("enabled_skills")
    with op.batch_alter_table("memory_facts", schema=None) as batch_op:
        batch_op.drop_index("idx_memory_facts_user_agent_active")
        batch_op.drop_index("idx_memory_facts_thread_id")
        batch_op.drop_index("idx_memory_facts_category")
    op.drop_table("memory_facts")
