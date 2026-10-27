"""add builtin Skill binding fields to app configs

Revision ID: d_v2_005_create_skills
Revises: d_v2_004_add_subagents
Create Date: 2026-07-28 22:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d_v2_005_create_skills"
down_revision = "d_v2_004_add_subagents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("app_config", "app_config_version"):
        op.add_column(
            table,
            sa.Column(
                "skills",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )


def downgrade() -> None:
    for table in ("app_config", "app_config_version"):
        op.drop_column(table, "skills")
