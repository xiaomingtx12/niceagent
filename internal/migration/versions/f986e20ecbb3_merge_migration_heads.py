"""merge migration heads

Revision ID: f986e20ecbb3
Revises: 223e8bdd31a6, 6837222596d1
Create Date: 2026-02-28 17:42:05.291094

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f986e20ecbb3'
down_revision = ('223e8bdd31a6', '6837222596d1')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
