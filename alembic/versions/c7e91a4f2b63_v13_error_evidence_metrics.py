"""v1.3 解析错误、AI 证据与检索指标（迁移⑨）。

Revision ID: c7e91a4f2b63
Revises: b4d8e2f60a17
Create Date: 2026-10-25 00:00:00.000000
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c7e91a4f2b63"
down_revision = "b4d8e2f60a17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("parse_error", sa.Text(), nullable=True))
    op.add_column(
        "posts",
        sa.Column("ai_citation_chunk_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "qa_logs",
        sa.Column("retrieval_metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("qa_logs", "retrieval_metrics")
    op.drop_column("posts", "ai_citation_chunk_ids")
    op.drop_column("documents", "parse_error")
