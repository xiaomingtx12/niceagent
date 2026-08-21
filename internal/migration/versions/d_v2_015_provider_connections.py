"""add explicit provider connection identity to model bindings

Revision ID: d_v2_015_provider_connections
Revises: 169a488e20cb
Create Date: 2026-08-22

"""
from alembic import op
import sqlalchemy as sa


revision = "d_v2_015_provider_connections"
down_revision = "169a488e20cb"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "dataset",
        sa.Column("embedding_provider_connection_id", sa.UUID(), nullable=True),
    )


def downgrade():
    op.drop_column("dataset", "embedding_provider_connection_id")
