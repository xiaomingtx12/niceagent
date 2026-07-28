"""add DeepAgents subagent config to app configurations

Revision ID: d_v2_004_add_subagents
Revises: d_v2_003_create_memory_facts
Create Date: 2026-07-28 21:00:00.000000

The platform stores declarative synchronous sub-agents alongside tools and
workflows. DeepAgents compiles these records through its native
`SubAgentMiddleware`; no separate agent/member table is required.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d_v2_004_add_subagents"
down_revision = "d_v2_003_create_memory_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add additive JSONB team configuration with an empty-team default."""
    subagents_type = postgresql.JSONB(astext_type=sa.Text())
    op.add_column(
        "app_config",
        sa.Column(
            "subagents",
            subagents_type,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "app_config_version",
        sa.Column(
            "subagents",
            subagents_type,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Remove the additive team configuration columns."""
    op.drop_column("app_config_version", "subagents")
    op.drop_column("app_config", "subagents")
