"""add V2 resource foundation

Revision ID: f1a2b3c4d5e6
Revises: d5b1c3e7f9a2
Create Date: 2026-07-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "f1a2b3c4d5e6"
down_revision = "d5b1c3e7f9a2"
branch_labels = None
depends_on = None


def _timestamps():
    return (
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP(0)"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP(0)"),
            nullable=False,
        ),
    )


def _uuid(name):
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=False),
        server_default=sa.text("uuid_generate_v4()"),
        nullable=False,
    )


def upgrade():
    op.create_table(
        "agent_definition",
        _uuid("id"),
        sa.Column("account_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("latest_version_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("name", sa.String(255), server_default=sa.text("''"), nullable=False),
        sa.Column("icon", sa.String(255), server_default=sa.text("''"), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.String(32), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_agent_definition_id"),
    )
    op.create_index("agent_definition_account_id_idx", "agent_definition", ["account_id"])
    op.create_index("agent_definition_status_idx", "agent_definition", ["status"])

    op.create_table(
        "agent_draft",
        _uuid("id"),
        sa.Column("agent_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_agent_draft_id"),
        sa.UniqueConstraint("agent_id", name="agent_draft_agent_id_uq"),
    )
    op.create_index("agent_draft_agent_id_idx", "agent_draft", ["agent_id"])

    op.create_table(
        "agent_version",
        _uuid("id"),
        sa.Column("agent_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP(0)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_version_id"),
        sa.UniqueConstraint("agent_id", "version", name="agent_version_agent_id_version_uq"),
    )
    op.create_index("agent_version_agent_id_idx", "agent_version", ["agent_id"])

    op.create_table(
        "team_definition",
        _uuid("id"),
        sa.Column("account_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("latest_version_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("name", sa.String(255), server_default=sa.text("''"), nullable=False),
        sa.Column("icon", sa.String(255), server_default=sa.text("''"), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.String(32), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_team_definition_id"),
    )
    op.create_index("team_definition_account_id_idx", "team_definition", ["account_id"])
    op.create_index("team_definition_status_idx", "team_definition", ["status"])

    op.create_table(
        "team_draft",
        _uuid("id"),
        sa.Column("team_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("lead_agent_version_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("instructions", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "execution_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_team_draft_id"),
        sa.UniqueConstraint("team_id", name="team_draft_team_id_uq"),
    )
    op.create_index("team_draft_team_id_idx", "team_draft", ["team_id"])

    op.create_table(
        "team_draft_member",
        _uuid("id"),
        sa.Column("team_draft_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("agent_version_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("role_name", sa.String(255), server_default=sa.text("''"), nullable=False),
        sa.Column("role_prompt_override", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "context_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "capability_restrictions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_team_draft_member_id"),
    )
    op.create_index(
        "team_draft_member_team_draft_id_idx", "team_draft_member", ["team_draft_id"]
    )
    op.create_index(
        "team_draft_member_agent_version_id_idx",
        "team_draft_member",
        ["agent_version_id"],
    )

    op.create_table(
        "team_version",
        _uuid("id"),
        sa.Column("team_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("lead_agent_version_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("instructions", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "execution_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP(0)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_team_version_id"),
        sa.UniqueConstraint("team_id", "version", name="team_version_team_id_version_uq"),
    )
    op.create_index("team_version_team_id_idx", "team_version", ["team_id"])

    op.create_table(
        "team_version_member",
        _uuid("id"),
        sa.Column("team_version_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("agent_version_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("role_name", sa.String(255), server_default=sa.text("''"), nullable=False),
        sa.Column("role_prompt_override", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "context_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "capability_restrictions",
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
        sa.PrimaryKeyConstraint("id", name="pk_team_version_member_id"),
    )
    op.create_index(
        "team_version_member_team_version_id_idx",
        "team_version_member",
        ["team_version_id"],
    )
    op.create_index(
        "team_version_member_agent_version_id_idx",
        "team_version_member",
        ["agent_version_id"],
    )

    op.create_table(
        "application",
        _uuid("id"),
        sa.Column("account_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("latest_version_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("active_version_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("name", sa.String(255), server_default=sa.text("''"), nullable=False),
        sa.Column("icon", sa.String(255), server_default=sa.text("''"), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.String(32), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_application_id"),
    )
    op.create_index("application_account_id_idx", "application", ["account_id"])
    op.create_index("application_status_idx", "application", ["status"])

    op.create_table(
        "application_draft",
        _uuid("id"),
        sa.Column("application_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_version_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "presentation_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "channel_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "access_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_application_draft_id"),
        sa.UniqueConstraint("application_id", name="application_draft_application_id_uq"),
    )
    op.create_index(
        "application_draft_application_id_idx",
        "application_draft",
        ["application_id"],
    )

    op.create_table(
        "application_version",
        _uuid("id"),
        sa.Column("application_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_version_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "presentation_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "channel_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "access_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP(0)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_application_version_id"),
        sa.UniqueConstraint(
            "application_id",
            "version",
            name="application_version_application_id_version_uq",
        ),
    )
    op.create_index(
        "application_version_application_id_idx",
        "application_version",
        ["application_id"],
    )

    op.create_table(
        "legacy_resource_map",
        _uuid("id"),
        sa.Column("legacy_app_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "source_checksum",
            sa.String(128),
            server_default=sa.text("''"),
            nullable=False,
        ),
        sa.Column(
            "sync_status",
            sa.String(32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("last_error", sa.String(2048), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_legacy_resource_map_id"),
        sa.UniqueConstraint(
            "legacy_app_id",
            "resource_type",
            name="legacy_resource_map_legacy_app_resource_uq",
        ),
        sa.UniqueConstraint(
            "resource_type",
            "resource_id",
            name="legacy_resource_map_resource_identity_uq",
        ),
    )
    op.create_index(
        "legacy_resource_map_legacy_app_id_idx",
        "legacy_resource_map",
        ["legacy_app_id"],
    )
    op.create_index(
        "legacy_resource_map_sync_status_idx",
        "legacy_resource_map",
        ["sync_status"],
    )


def downgrade():
    for table in (
        "legacy_resource_map",
        "application_version",
        "application_draft",
        "application",
        "team_version_member",
        "team_version",
        "team_draft_member",
        "team_draft",
        "team_definition",
        "agent_version",
        "agent_draft",
        "agent_definition",
    ):
        op.drop_table(table)
