"""fix app table id column uuid default value

Revision ID: 999999999999
Revises: 88998d2b7a48
Create Date: 2026-01-24 21:53:36.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '999999999999'
down_revision = '88998d2b7a48'
branch_labels = None
depends_on = None


def upgrade():
    # Ensure uuid-ossp extension is available
    op.execute(sa.text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'))
    
    # Set default value for the app table id column to use uuid_generate_v4()
    # First, update existing null values to new UUIDs if any
    op.execute("UPDATE app SET id = uuid_generate_v4() WHERE id IS NULL;")
    
    # Then alter the column to have a default value
    op.alter_column('app', 'id',
                   server_default=sa.text('uuid_generate_v4()'),
                   nullable=False)


def downgrade():
    # Remove the default value
    op.alter_column('app', 'id',
                   server_default=None,
                   nullable=False)