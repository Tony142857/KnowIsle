"""ORM 模型（文档 §8 完整 DDL：6 组 24 张表）。

TODO(v0.1): 按 §8.2-8.7 定义全部 ORM 模型，并用 Alembic 生成初始迁移。
表清单：majors / users / auth_identities / semesters / courses / chapters /
documents / chunks / resources / review_tasks / review_records / posts /
comments / votes / favorites / follows / reports / score_logs / credit_logs /
ai_quotas / user_llm_keys / qa_logs / notifications / audit_logs
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
