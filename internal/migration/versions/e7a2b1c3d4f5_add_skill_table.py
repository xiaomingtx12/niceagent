"""add skill table

Revision ID: e7a2b1c3d4f5
Revises: d5b1c3e7f9a2
Create Date: 2026-07-19 18:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "e7a2b1c3d4f5"
down_revision = "d5b1c3e7f9a2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "skill",
        sa.Column(
            "id",
            sa.String(length=36),
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("owner_tenant_id", sa.String(length=36), nullable=True),
        sa.Column(
            "category",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'tenant'"),
        ),
        sa.Column(
            "visibility",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'private'"),
        ),
        sa.Column(
            "version",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'1.0.0'"),
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column(
            "description",
            sa.String(length=512),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("frontmatter", sa.JSON(), nullable=False),
        sa.Column(
            "body",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_skill_id"),
    )
    with op.batch_alter_table("skill", schema=None) as batch_op:
        batch_op.create_index(
            "skill_owner_name_idx",
            ["owner_tenant_id", "name"],
            unique=True,
        )
        batch_op.create_index("skill_status_idx", ["status"], unique=False)
        batch_op.create_index("skill_category_idx", ["category"], unique=False)


def downgrade():
    with op.batch_alter_table("skill", schema=None) as batch_op:
        batch_op.drop_index("skill_category_idx")
        batch_op.drop_index("skill_status_idx")
        batch_op.drop_index("skill_owner_name_idx")
    op.drop_table("skill")
