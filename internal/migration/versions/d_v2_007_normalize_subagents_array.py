"""normalize subagent configuration to the final list schema

Revision ID: d_v2_007_subagents_array
Revises: d_v2_006_finalize_memory
Create Date: 2026-07-28 23:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "d_v2_007_subagents_array"
down_revision = "d_v2_006_finalize_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("app_config", "app_config_version"):
        op.execute(
            sa.text(
                f"""
                UPDATE {table}
                SET subagents = (
                    SELECT COALESCE(
                        jsonb_agg(jsonb_build_object('name', key) || value),
                        '[]'::jsonb
                    )
                    FROM jsonb_each(subagents)
                )
                WHERE jsonb_typeof(subagents) = 'object'
                """
            )
        )
        op.alter_column(
            table,
            "subagents",
            server_default=sa.text("'[]'::jsonb"),
            existing_nullable=False,
        )


def downgrade() -> None:
    for table in ("app_config", "app_config_version"):
        op.execute(
            sa.text(
                f"""
                UPDATE {table}
                SET subagents = (
                    SELECT COALESCE(
                        jsonb_object_agg(item->>'name', item - 'name'),
                        '{{}}'::jsonb
                    )
                    FROM jsonb_array_elements(subagents) AS item
                )
                WHERE jsonb_typeof(subagents) = 'array'
                """
            )
        )
        op.alter_column(
            table,
            "subagents",
            server_default=sa.text("'{}'::jsonb"),
            existing_nullable=False,
        )
