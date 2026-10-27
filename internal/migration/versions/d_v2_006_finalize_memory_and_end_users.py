"""finalize structured memory fields and WebApp end-user identity

Revision ID: d_v2_006_finalize_memory
Revises: d_v2_005_create_skills
Create Date: 2026-07-28 22:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "d_v2_006_finalize_memory"
down_revision = "d_v2_005_create_skills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "memory_facts",
        "confidence",
        new_column_name="importance",
        existing_type=sa.Float(),
        existing_nullable=False,
        existing_server_default=sa.text("1.0"),
    )
    op.add_column(
        "memory_facts",
        sa.Column("source_message_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "memory_facts",
        sa.Column(
            "source_type",
            sa.String(length=16),
            server_default=sa.text("'auto'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_memory_facts_source_type",
        "memory_facts",
        "source_type IN ('auto', 'manual', 'edited')",
    )
    op.add_column(
        "end_user",
        sa.Column("external_user_id", sa.UUID(), nullable=True),
    )
    op.create_index(
        "uq_end_user_app_external",
        "end_user",
        ["app_id", "external_user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_end_user_app_external", table_name="end_user")
    op.drop_column("end_user", "external_user_id")
    op.drop_constraint(
        "ck_memory_facts_source_type",
        "memory_facts",
        type_="check",
    )
    op.drop_column("memory_facts", "source_type")
    op.drop_column("memory_facts", "source_message_id")
    op.alter_column(
        "memory_facts",
        "importance",
        new_column_name="confidence",
        existing_type=sa.Float(),
        existing_nullable=False,
        existing_server_default=sa.text("1.0"),
    )
