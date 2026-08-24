"""在已保留的自动生成 revision 后重建 Agent 工作台表。

Revision ID: d_v2_017_rebuild_workbench
Revises: 1df2bf5b63b9
Create Date: 2026-08-23

部分数据库已记录 ``1df2bf5b63b9``，却没有实际执行随后插入迁移链的
``d_v2_016_agent_workbench_runs``。工作台 Run 数据无需保留，因此按模型重建两张新表。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "d_v2_017_rebuild_workbench"
down_revision = "1df2bf5b63b9"
branch_labels = None
depends_on = None


def _uuid(name: str):
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=False),
        server_default=sa.text("uuid_generate_v4()"),
        nullable=False,
    )


def _timestamps():
    return (
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP(0)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP(0)"),
            nullable=False,
        ),
    )


def _add_visibility_column(table_name: str) -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if "workbench_visibility_mode" not in columns:
        op.add_column(
            table_name,
            sa.Column(
                "workbench_visibility_mode",
                sa.String(32),
                server_default=sa.text("'progress'"),
                nullable=False,
            ),
        )


def upgrade():
    """重建不需要保留数据的工作台运行表。"""
    inspector = sa.inspect(op.get_bind())
    table_names = set(inspector.get_table_names())
    if "agent_run_event" in table_names:
        op.drop_table("agent_run_event")
    if "agent_run" in table_names:
        op.drop_table("agent_run")

    _add_visibility_column("app_config")
    _add_visibility_column("app_config_version")

    op.create_table(
        "agent_run",
        _uuid("id"),
        sa.Column("app_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("thread_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("scope_kind", sa.String(32), nullable=False),
        sa.Column("scope_value", sa.String(255), nullable=False),
        sa.Column(
            "invoke_from", sa.String(255), server_default=sa.text("''"), nullable=False
        ),
        sa.Column(
            "visibility_mode",
            sa.String(32),
            server_default=sa.text("'progress'"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column(
            "status",
            sa.String(32),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column("query", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "values",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "last_sequence", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("error", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_agent_run_id"),
    )
    op.create_index(
        "agent_run_conversation_created_idx",
        "agent_run",
        ["conversation_id", "created_at"],
    )
    op.create_index(
        "agent_run_scope_thread_idx",
        "agent_run",
        ["scope_kind", "scope_value", "thread_id"],
    )

    op.create_table(
        "agent_run_event",
        _uuid("id"),
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(255), nullable=False),
        sa.Column("method", sa.String(64), nullable=False),
        sa.Column(
            "namespace",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP(0)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_run_event_id"),
        sa.UniqueConstraint("run_id", "seq", name="agent_run_event_run_seq_uq"),
        sa.UniqueConstraint(
            "run_id", "event_id", name="agent_run_event_run_event_uq"
        ),
    )
    op.create_index(
        "agent_run_event_run_seq_idx", "agent_run_event", ["run_id", "seq"]
    )


def downgrade():
    """前一 revision 的规范 schema 与本 revision 一致，无需反向改表。"""
    pass
