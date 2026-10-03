"""users_muted_until

Revision ID: d1e5f7a29c03
Revises: 9cbb7dcfd2f3
Create Date: 2026-10-01 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd1e5f7a29c03'
down_revision = '9cbb7dcfd2f3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('muted_until', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'muted_until')
