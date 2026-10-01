"""perf_indexes（v0.9 性能优化 A1）

Revision ID: e5f2b8c41d09
Revises: d1e5f7a29c03
Create Date: 2026-10-01 12:00:00.000000

补齐高频查询缺失的二级索引（既有索引见 e2e8ae0dcb8e 建表迁移，此处不重复建）：
- chunks(document_id)：解析重试清旧切块、克隆/派生按文档取切块
- comments(post_id)：帖子详情/评论列表
- votes(target_type, target_id)：帖子/评论/资源点赞聚合（既有唯一约束以 user_id 前导，覆盖不到）
- follows(target_type, target_id)：课程关注者查询
- documents(storage_key)：终审派生 public 副本按存储键定位
- review_tasks(resource_id) / review_records(task_id)：审核链路回查
- qa_logs(created_at)：运营看板按时间聚合
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'e5f2b8c41d09'
down_revision = 'd1e5f7a29c03'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('idx_chunks_document', 'chunks', ['document_id'], unique=False)
    op.create_index('idx_comments_post', 'comments', ['post_id'], unique=False)
    op.create_index('idx_votes_target', 'votes', ['target_type', 'target_id'], unique=False)
    op.create_index('idx_follows_target', 'follows', ['target_type', 'target_id'], unique=False)
    op.create_index('idx_documents_storage_key', 'documents', ['storage_key'], unique=False)
    op.create_index('idx_review_tasks_resource', 'review_tasks', ['resource_id'], unique=False)
    op.create_index('idx_review_records_task', 'review_records', ['task_id'], unique=False)
    op.create_index('idx_qa_logs_created_at', 'qa_logs', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_qa_logs_created_at', table_name='qa_logs')
    op.drop_index('idx_review_records_task', table_name='review_records')
    op.drop_index('idx_review_tasks_resource', table_name='review_tasks')
    op.drop_index('idx_documents_storage_key', table_name='documents')
    op.drop_index('idx_follows_target', table_name='follows')
    op.drop_index('idx_votes_target', table_name='votes')
    op.drop_index('idx_comments_post', table_name='comments')
    op.drop_index('idx_chunks_document', table_name='chunks')
