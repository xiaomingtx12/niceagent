"""add skill.tool_names

Revision ID: a1b2c3d4e5f6
Revises: 7499bc806654
Create Date: 2026-07-20 16:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "7499bc806654"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("skill", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tool_names",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
                comment="该 skill 显式声明需要的工具名列表（运行时由 AppConfig 装配层 resolve）",
            )
        )


def downgrade():
    with op.batch_alter_table("skill", schema=None) as batch_op:
        batch_op.drop_column("tool_names")
