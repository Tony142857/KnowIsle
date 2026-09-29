"""resource_ratings

Revision ID: b7f3a1c94e02
Revises: e2e8ae0dcb8e
Create Date: 2026-09-29 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b7f3a1c94e02'
down_revision = 'e2e8ae0dcb8e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('resource_ratings',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('resource_id', sa.BigInteger(), nullable=False),
    sa.Column('stars', sa.SmallInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('stars BETWEEN 1 AND 5', name='ck_resource_ratings_stars'),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'resource_id')
    )


def downgrade() -> None:
    op.drop_table('resource_ratings')
