"""为模型 API Key 配置增加自定义服务地址。"""

from alembic import op
import sqlalchemy as sa


revision = "d_v2_013_provider_base_url"
down_revision = "d_v2_012_message_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加用户级 OpenAI-compatible 服务地址。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"]
        for column in inspector.get_columns("provider_api_key")
    }
    if "base_url" not in columns:
        op.add_column(
            "provider_api_key",
            sa.Column(
                "base_url",
                sa.String(length=1000),
                nullable=False,
                server_default=sa.text("''::character varying"),
            ),
        )


def downgrade() -> None:
    """移除用户级模型服务地址。"""
    op.drop_column("provider_api_key", "base_url")
