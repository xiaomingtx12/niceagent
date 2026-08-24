"""remove abandoned workbench visibility configuration

Revision ID: d_v2_018_remove_workbench_visibility
Revises: d_v2_017_rebuild_workbench
Create Date: 2026-08-23
"""

from alembic import op
import sqlalchemy as sa


revision = "d_v2_018_remove_workbench_visibility"
down_revision = "d_v2_017_rebuild_workbench"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def upgrade() -> None:
    """移除已经落库但不再使用的可见模式字段。"""
    for table_name, column_name in (
        ("agent_run", "visibility_mode"),
        ("app_config", "workbench_visibility_mode"),
        ("app_config_version", "workbench_visibility_mode"),
    ):
        if _has_column(table_name, column_name):
            op.drop_column(table_name, column_name)


def downgrade() -> None:
    """恢复上一 revision 的字段，便于迁移链回退。"""
    for table_name, column_name in (
        ("app_config", "workbench_visibility_mode"),
        ("app_config_version", "workbench_visibility_mode"),
        ("agent_run", "visibility_mode"),
    ):
        if not _has_column(table_name, column_name):
            op.add_column(
                table_name,
                sa.Column(
                    column_name,
                    sa.String(32),
                    server_default=sa.text("'progress'"),
                    nullable=False,
                ),
            )
