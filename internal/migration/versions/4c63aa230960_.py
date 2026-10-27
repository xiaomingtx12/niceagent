"""精简 Agent 工作台运行与事件持久化结构。

Revision ID: 4c63aa230960
Revises: d_v2_017_rebuild_workbench
Create Date: 2026-08-26 00:51:32.588845
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "4c63aa230960"
down_revision = "d_v2_017_rebuild_workbench"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留有效历史数据，并迁移到精简后的工作台结构。"""
    # 旧 scope 字段仅用于区分调用来源，先投影到保留的 invoke_from。
    op.execute(
        """
        UPDATE agent_run
        SET invoke_from = CASE scope_kind
            WHEN 'app' THEN 'debugger'
            WHEN 'web_app' THEN 'web_app'
            ELSE invoke_from
        END
        """
    )

    # 无消息归属的旧 Run 无法满足新模型约束，同时清理其事件。
    op.execute(
        """
        DELETE FROM agent_run_event
        WHERE run_id IN (
            SELECT id FROM agent_run WHERE message_id IS NULL
        )
        """
    )
    op.execute("DELETE FROM agent_run WHERE message_id IS NULL")

    # 唯一索引创建前，只保留同一线程最新的活动 Run。
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY thread_id
                       ORDER BY created_at DESC, id DESC
                   ) AS position
            FROM agent_run
            WHERE status IN ('queued', 'running')
        )
        UPDATE agent_run AS run
        SET status = 'failed',
            error = '迁移时结束同一线程的重复活动 Run',
            finished_at = COALESCE(run.finished_at, CURRENT_TIMESTAMP)
        FROM ranked
        WHERE run.id = ranked.id
          AND ranked.position > 1
        """
    )

    op.drop_index("agent_run_conversation_created_idx", table_name="agent_run")
    op.drop_index("agent_run_scope_thread_idx", table_name="agent_run")
    op.add_column(
        "agent_run",
        sa.Column("input_message_id", sa.String(255), nullable=True),
    )
    op.alter_column(
        "agent_run",
        "values",
        new_column_name="state_snapshot",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=False,
        existing_server_default=sa.text("'{}'::jsonb"),
    )
    op.alter_column(
        "agent_run",
        "last_sequence",
        new_column_name="last_event_sequence",
        existing_type=sa.Integer(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "agent_run",
        "error",
        new_column_name="error_message",
        existing_type=sa.Text(),
        existing_nullable=False,
        existing_server_default=sa.text("''::text"),
    )
    op.alter_column(
        "agent_run",
        "invoke_from",
        existing_type=sa.String(255),
        type_=sa.String(32),
        existing_nullable=False,
        existing_server_default=sa.text("''::character varying"),
        server_default=None,
    )
    op.alter_column(
        "agent_run",
        "message_id",
        existing_type=postgresql.UUID(as_uuid=False),
        existing_nullable=True,
        nullable=False,
    )
    for column_name in (
        "conversation_id",
        "scope_kind",
        "scope_value",
        "visibility_mode",
        "created_by",
        "query",
    ):
        op.drop_column("agent_run", column_name)

    op.create_index(
        "agent_run_thread_source_created_idx",
        "agent_run",
        ["thread_id", "invoke_from", "created_at"],
    )
    op.create_index(
        "agent_run_thread_executing_uq",
        "agent_run",
        ["thread_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "agent_run_thread_input_message_uq",
        "agent_run",
        ["thread_id", "input_message_id"],
        unique=True,
        postgresql_where=sa.text("input_message_id IS NOT NULL"),
    )

    # seq 是 sequence 的旧名，重命名可完整保留历史事件。
    op.drop_index("agent_run_event_run_seq_idx", table_name="agent_run_event")
    op.drop_constraint(
        "agent_run_event_run_event_uq",
        "agent_run_event",
        type_="unique",
    )
    op.drop_constraint(
        "agent_run_event_run_seq_uq",
        "agent_run_event",
        type_="unique",
    )
    op.drop_constraint("pk_agent_run_event_id", "agent_run_event", type_="primary")
    op.alter_column(
        "agent_run_event",
        "seq",
        new_column_name="sequence",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    for column_name in ("id", "event_id", "method", "namespace"):
        op.drop_column("agent_run_event", column_name)
    op.create_primary_key(
        "pk_agent_run_event_run_sequence",
        "agent_run_event",
        ["run_id", "sequence"],
    )

    op.drop_column("app_config", "workbench_visibility_mode")
    op.drop_column("app_config_version", "workbench_visibility_mode")


def downgrade() -> None:
    """恢复旧表结构；已删除的冗余业务值使用稳定字段回填。"""
    op.add_column(
        "app_config_version",
        sa.Column(
            "workbench_visibility_mode",
            sa.String(32),
            server_default=sa.text("'progress'"),
            nullable=False,
        ),
    )
    op.add_column(
        "app_config",
        sa.Column(
            "workbench_visibility_mode",
            sa.String(32),
            server_default=sa.text("'progress'"),
            nullable=False,
        ),
    )

    op.drop_constraint(
        "pk_agent_run_event_run_sequence",
        "agent_run_event",
        type_="primary",
    )
    op.alter_column(
        "agent_run_event",
        "sequence",
        new_column_name="seq",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.add_column(
        "agent_run_event",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
    )
    op.add_column(
        "agent_run_event",
        sa.Column(
            "event_id",
            sa.String(255),
            server_default=sa.text("''"),
            nullable=False,
        ),
    )
    op.add_column(
        "agent_run_event",
        sa.Column(
            "method",
            sa.String(64),
            server_default=sa.text("''"),
            nullable=False,
        ),
    )
    op.add_column(
        "agent_run_event",
        sa.Column(
            "namespace",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE agent_run_event
        SET event_id = COALESCE(
                NULLIF(payload ->> 'event_id', ''),
                run_id::text || ':' || seq::text
            ),
            method = COALESCE(NULLIF(payload ->> 'method', ''), 'custom'),
            namespace = CASE
                WHEN jsonb_typeof(payload -> 'params' -> 'namespace') = 'array'
                    THEN payload -> 'params' -> 'namespace'
                ELSE '[]'::jsonb
            END
        """
    )
    op.alter_column(
        "agent_run_event",
        "event_id",
        existing_type=sa.String(255),
        existing_nullable=False,
        existing_server_default=sa.text("''::character varying"),
        server_default=None,
    )
    op.alter_column(
        "agent_run_event",
        "method",
        existing_type=sa.String(64),
        existing_nullable=False,
        existing_server_default=sa.text("''::character varying"),
        server_default=None,
    )
    op.create_primary_key("pk_agent_run_event_id", "agent_run_event", ["id"])
    op.create_unique_constraint(
        "agent_run_event_run_seq_uq",
        "agent_run_event",
        ["run_id", "seq"],
    )
    op.create_unique_constraint(
        "agent_run_event_run_event_uq",
        "agent_run_event",
        ["run_id", "event_id"],
    )
    op.create_index(
        "agent_run_event_run_seq_idx",
        "agent_run_event",
        ["run_id", "seq"],
    )

    op.drop_index("agent_run_thread_input_message_uq", table_name="agent_run")
    op.drop_index("agent_run_thread_executing_uq", table_name="agent_run")
    op.drop_index("agent_run_thread_source_created_idx", table_name="agent_run")
    op.alter_column(
        "agent_run",
        "state_snapshot",
        new_column_name="values",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=False,
        existing_server_default=sa.text("'{}'::jsonb"),
    )
    op.alter_column(
        "agent_run",
        "last_event_sequence",
        new_column_name="last_sequence",
        existing_type=sa.Integer(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "agent_run",
        "error_message",
        new_column_name="error",
        existing_type=sa.Text(),
        existing_nullable=False,
        existing_server_default=sa.text("''::text"),
    )
    op.alter_column(
        "agent_run",
        "invoke_from",
        existing_type=sa.String(32),
        type_=sa.String(255),
        existing_nullable=False,
        existing_server_default=None,
        server_default=sa.text("''"),
    )
    op.alter_column(
        "agent_run",
        "message_id",
        existing_type=postgresql.UUID(as_uuid=False),
        existing_nullable=False,
        nullable=True,
    )
    op.add_column(
        "agent_run",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=False), nullable=True),
    )
    op.add_column(
        "agent_run",
        sa.Column("scope_kind", sa.String(32), nullable=True),
    )
    op.add_column(
        "agent_run",
        sa.Column("scope_value", sa.String(255), nullable=True),
    )
    op.add_column(
        "agent_run",
        sa.Column(
            "visibility_mode",
            sa.String(32),
            server_default=sa.text("'progress'"),
            nullable=False,
        ),
    )
    op.add_column(
        "agent_run",
        sa.Column("created_by", postgresql.UUID(as_uuid=False), nullable=True),
    )
    op.add_column(
        "agent_run",
        sa.Column(
            "query",
            sa.Text(),
            server_default=sa.text("''"),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE agent_run
        SET conversation_id = thread_id,
            scope_kind = CASE invoke_from
                WHEN 'debugger' THEN 'app'
                ELSE 'web_app'
            END,
            scope_value = app_id::text
        """
    )
    op.alter_column(
        "agent_run",
        "conversation_id",
        existing_type=postgresql.UUID(as_uuid=False),
        existing_nullable=True,
        nullable=False,
    )
    op.alter_column(
        "agent_run",
        "scope_kind",
        existing_type=sa.String(32),
        existing_nullable=True,
        nullable=False,
    )
    op.alter_column(
        "agent_run",
        "scope_value",
        existing_type=sa.String(255),
        existing_nullable=True,
        nullable=False,
    )
    op.drop_column("agent_run", "input_message_id")
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
