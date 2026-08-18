"""add app_type and app_member table

Revision ID: b3f2a1c8d4e5
Revises: fe593d5bb1ab
Create Date: 2026-07-08 20:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b3f2a1c8d4e5"
down_revision = "fe593d5bb1ab"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("app", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "app_type",
                sa.String(length=255),
                nullable=False,
                server_default=sa.text("'chat'::character varying"),
                comment="应用类型(chat普通对话/multi_agent多Agent协作)",
            )
        )

    op.create_table(
        "app_member",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column(
            "supervisor_app_id",
            postgresql.UUID(as_uuid=False),
            nullable=False,
            comment="Supervisor应用id",
        ),
        sa.Column(
            "member_app_id",
            postgresql.UUID(as_uuid=False),
            nullable=False,
            comment="子Agent应用id（独立App）",
        ),
        sa.Column(
            "name",
            sa.String(length=255),
            nullable=False,
            server_default=sa.text("''::character varying"),
            comment="工具名（tool.name）",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''::text"),
            comment="能力描述（供路由决策）",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment="是否启用",
        ),
        sa.Column(
            "order",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="排序",
        ),
        sa.Column(
            "context_passing",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="上下文传递配置",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP(0)"),
            nullable=False,
            server_onupdate=sa.text("CURRENT_TIMESTAMP(0)"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP(0)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_app_member_id"),
    )
    with op.batch_alter_table("app_member", schema=None) as batch_op:
        batch_op.create_index(
            "app_member_supervisor_app_id_idx",
            ["supervisor_app_id"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("app_member", schema=None) as batch_op:
        batch_op.drop_index("app_member_supervisor_app_id_idx")
    op.drop_table("app_member")
    with op.batch_alter_table("app", schema=None) as batch_op:
        batch_op.drop_column("app_type")
