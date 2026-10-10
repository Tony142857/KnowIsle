"""resources_approved_at（§20.5 B3，v1.1 迁移⑧）

Revision ID: b4d8e2f60a17
Revises: f3a7c91d2e05
Create Date: 2026-10-10 12:01:00.000000

resources 新增 approved_at TIMESTAMPTZ（NULL=未上架/存量数据）：feed
「最新上架」按 approved_at DESC NULLS LAST 排序；终审/直审通过路径回填
now()。存量已上架资源回填：取该资源终审任务（review_tasks.finished_at
最大者）的完成时间，取不到则保留 NULL。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b4d8e2f60a17'
down_revision = 'f3a7c91d2e05'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('resources', sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True))
    # 存量 approved 资源回填：终审任务的 finished_at 可取则取，否则保留 NULL
    op.execute(
        """
        UPDATE resources r
        SET approved_at = (
            SELECT max(rt.finished_at) FROM review_tasks rt
            WHERE rt.resource_id = r.id
        )
        WHERE r.review_status = 'approved'
        """
    )


def downgrade() -> None:
    op.drop_column('resources', 'approved_at')
