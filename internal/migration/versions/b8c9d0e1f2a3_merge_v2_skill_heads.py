"""merge skill and V2 migration heads

Revision ID: b8c9d0e1f2a3
Revises: a1b2c3d4e5f6, f1a2b3c4d5e6
Create Date: 2026-07-21 22:28:00.000000

This is a no-op merge that consolidates the Skill platform head and the V2
resource foundation head into one migration branch.
"""


revision = "b8c9d0e1f2a3"
down_revision = ("a1b2c3d4e5f6", "f1a2b3c4d5e6")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
