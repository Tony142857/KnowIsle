"""documents_md5_personal_unique（§20.5 B1，v1.1 迁移⑦）

Revision ID: f3a7c91d2e05
Revises: e5f2b8c41d09
Create Date: 2026-10-10 12:00:00.000000

documents 新增部分唯一索引 UNIQUE(owner_id, md5) WHERE scope='personal'：
个人库上传 MD5 去重的并发兜底（先查后插的竞态由唯一索引收口，路由层
捕获 IntegrityError 回落既有去重响应）。公共派生副本（终审通过时复制，
同 owner 同 md5）不受限，故必须 partial。
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'f3a7c91d2e05'
down_revision = 'e5f2b8c41d09'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        'uq_documents_owner_md5_personal', 'documents', ['owner_id', 'md5'],
        unique=True, postgresql_where="scope='personal'",
    )


def downgrade() -> None:
    op.drop_index('uq_documents_owner_md5_personal', table_name='documents')
