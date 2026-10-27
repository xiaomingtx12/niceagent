"""为消息记录增加 Agent Run 级 Token 与调用统计字段。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d_v2_010_agent_usage_fields"
down_revision = "b3c416557a96"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为旧数据库安全补充可观测性统计字段。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"] for column in inspector.get_columns("message")
    }
    definitions = (
        (
            "input_token_count",
            sa.Column(
                "input_token_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        ),
        (
            "output_token_count",
            sa.Column(
                "output_token_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        ),
        (
            "cached_token_count",
            sa.Column(
                "cached_token_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        ),
        (
            "reasoning_token_count",
            sa.Column(
                "reasoning_token_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        ),
        (
            "model_call_count",
            sa.Column(
                "model_call_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        ),
        (
            "tool_call_count",
            sa.Column(
                "tool_call_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        ),
        (
            "trace_id",
            sa.Column("trace_id", sa.String(length=64), nullable=True),
        ),
        (
            "token_usage",
            sa.Column(
                "token_usage",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        ),
    )
    for name, column in definitions:
        if name not in columns:
            op.add_column("message", column)


def downgrade() -> None:
    """移除消息级 Run 统计字段。"""
    for name in (
        "token_usage",
        "trace_id",
        "tool_call_count",
        "model_call_count",
        "reasoning_token_count",
        "cached_token_count",
        "output_token_count",
        "input_token_count",
    ):
        op.drop_column("message", name)
