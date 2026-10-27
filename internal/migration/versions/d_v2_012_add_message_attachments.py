"""为消息增加附件元数据字段。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d_v2_012_message_attachments"
down_revision = "e01d6b1bcea3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为旧数据库安全补充消息附件字段。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("message")}
    if "attachments" not in columns:
        op.add_column(
            "message",
            sa.Column(
                "attachments",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )


def downgrade() -> None:
    """移除消息附件字段。"""
    op.drop_column("message", "attachments")
