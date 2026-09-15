"""ORM 模型（文档 §8 完整 DDL：6 组 24 张表）。

PostgreSQL 16，Alembic 管理迁移。主键统一 BIGINT GENERATED ALWAYS AS IDENTITY。
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _id() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(always=True), primary_key=True)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# 8.2 用户与组织类
# ---------------------------------------------------------------------------


class Major(Base):
    __tablename__ = "majors"

    id: Mapped[int] = _id()
    name: Mapped[str] = mapped_column(Text, nullable=False)  # 专业名（建站时按名单导入）
    code: Mapped[str | None] = mapped_column(Text)  # 专业代码
    school: Mapped[str | None] = mapped_column(Text)  # 预留：多校扩展
    college: Mapped[str | None] = mapped_column(Text)  # 预留：多学院扩展
    created_at: Mapped[datetime] = _created_at()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('student','reviewer','builder','admin')", name="ck_users_role"
        ),
        CheckConstraint(
            "status IN ('active','muted','frozen')", name="ck_users_status"
        ),
    )

    id: Mapped[int] = _id()
    student_no: Mapped[str] = mapped_column(Text, unique=True, nullable=False)  # 学号，认证锚点
    real_name: Mapped[str] = mapped_column(Text, nullable=False)  # 仅认证核验用，不公开
    nickname: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    major_id: Mapped[int | None] = mapped_column(ForeignKey("majors.id"))
    grade: Mapped[str | None] = mapped_column(Text)  # 年级，如 2023 级
    role: Mapped[str] = mapped_column(Text, server_default="student", nullable=False)
    score: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)  # 贡献分
    level: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)  # 等级 1-6
    credit: Mapped[int] = mapped_column(Integer, server_default="100", nullable=False)  # 信用分
    gpa_public: Mapped[bool] = mapped_column(server_default="false", nullable=False)  # 学业画像授权
    status: Mapped[str] = mapped_column(Text, server_default="active", nullable=False)
    created_at: Mapped[datetime] = _created_at()


class AuthIdentity(Base):
    """认证身份绑定（cas / oidc / email_fallback），认证层切换不改业务代码（§3.1）。"""

    __tablename__ = "auth_identities"
    __table_args__ = (UniqueConstraint("provider", "external_id"),)

    id: Mapped[int] = _id()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)  # cas / oidc / email_fallback
    external_id: Mapped[str] = mapped_column(Text, nullable=False)  # 认证方唯一标识
    raw_profile: Mapped[dict | None] = mapped_column(JSONB)  # 学籍信息快照
    created_at: Mapped[datetime] = _created_at()


# ---------------------------------------------------------------------------
# 8.3 结构类
# ---------------------------------------------------------------------------


class Semester(Base):
    """个人库的学期维度。"""

    __tablename__ = "semesters"

    id: Mapped[int] = _id()
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)  # 如 "2025 秋"


class Course(Base):
    """课程空间：scope=public 属专业（社区），scope=personal 属用户（个人库）。"""

    __tablename__ = "courses"
    __table_args__ = (
        CheckConstraint("scope IN ('public','personal')", name="ck_courses_scope"),
        CheckConstraint(
            "status IN ('active','pending','disabled')", name="ck_courses_status"
        ),
        CheckConstraint(
            "(scope='public' AND major_id IS NOT NULL AND owner_id IS NULL) "
            "OR (scope='personal' AND owner_id IS NOT NULL)",
            name="ck_courses_scope_ownership",
        ),
        Index(
            "idx_courses_major", "major_id", postgresql_where=text("scope='public'")
        ),
        Index(
            "idx_courses_owner", "owner_id", postgresql_where=text("scope='personal'")
        ),
    )

    id: Mapped[int] = _id()
    major_id: Mapped[int | None] = mapped_column(ForeignKey("majors.id"))  # 公共课程空间所属专业
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))  # 个人库课程所属用户
    semester_id: Mapped[int | None] = mapped_column(ForeignKey("semesters.id"))  # 仅个人库
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="active", nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Chapter(Base):
    """章节知识树（结构化 RAG 骨架），parent_id 支持小节，根章节为 NULL。"""

    __tablename__ = "chapters"
    __table_args__ = (Index("idx_chapters_course", "course_id"),)

    id: Mapped[int] = _id()
    course_id: Mapped[int] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id"))
    order_idx: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    summary_vector_id: Mapped[str | None] = mapped_column(Text)  # 章节摘要向量 ID（粗召回用）


# ---------------------------------------------------------------------------
# 8.4 资源与检索类
# ---------------------------------------------------------------------------


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("scope IN ('personal','public')", name="ck_documents_scope"),
        CheckConstraint(
            "file_type IN ('pdf_textbook','ppt','word','markdown')",
            name="ck_documents_file_type",
        ),
        CheckConstraint(
            "status IN ('parsing','parsed','failed')", name="ck_documents_status"
        ),
        Index("idx_documents_md5", "md5"),
        Index("idx_documents_owner", "owner_id"),
    )

    id: Mapped[int] = _id()
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), nullable=False)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)  # 上传者
    scope: Mapped[str] = mapped_column(Text, server_default="personal", nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_type: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)  # 对象存储 S3 对象键
    preview_key: Mapped[str | None] = mapped_column(Text)  # 预览用 PDF 对象键
    md5: Mapped[str] = mapped_column(Text, nullable=False)  # 文件去重指纹
    status: Mapped[str] = mapped_column(Text, server_default="parsing", nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Chunk(Base):
    """文本块：双库隔离检索的基本单元（元数据示例见 §8.8）。"""

    __tablename__ = "chunks"
    __table_args__ = (
        CheckConstraint("scope IN ('personal','public')", name="ck_chunks_scope"),
        Index("idx_chunks_course_scope", "course_id", "scope"),
        Index(
            "idx_chunks_owner", "owner_id", postgresql_where=text("scope='personal'")
        ),
    )

    id: Mapped[int] = _id()
    chunk_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)  # 业务 ID
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), nullable=False)
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id"))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)  # 归属/署名
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[str | None] = mapped_column(Text)
    page_num: Mapped[int | None] = mapped_column(Integer)
    file_type: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_id: Mapped[str | None] = mapped_column(Text)  # 对应向量 ID


class Resource(Base):
    """公共库的社区展示层：审核通过的资料条目。"""

    __tablename__ = "resources"
    __table_args__ = (
        CheckConstraint(
            "review_status IN ('pending','co_reviewing','approved','rejected')",
            name="ck_resources_review_status",
        ),
        Index("idx_resources_course", "course_id", "review_status"),
    )

    id: Mapped[int] = _id()
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), nullable=False)
    uploader_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)  # 投稿人署名
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id"))  # 挂载章节
    review_status: Mapped[str] = mapped_column(Text, server_default="pending", nullable=False)
    download_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    fav_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    rating: Mapped[float | None] = mapped_column(Numeric(3, 2))
    rating_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    download_cost: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    created_at: Mapped[datetime] = _created_at()


# ---------------------------------------------------------------------------
# 8.5 审核类
# ---------------------------------------------------------------------------


class ReviewTask(Base):
    """三级审核任务（模块 B2）：precheck → co_review → final → done。"""

    __tablename__ = "review_tasks"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('precheck','co_review','final','done')", name="ck_review_tasks_stage"
        ),
    )

    id: Mapped[int] = _id()
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id"), nullable=False)
    stage: Mapped[str] = mapped_column(Text, server_default="precheck", nullable=False)
    precheck_result: Mapped[dict | None] = mapped_column(JSONB)  # 自动预检结果
    assignee_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))  # 被指派的协审员
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewRecord(Base):
    """协审/终审意见记录；驳回时 comment 必填。"""

    __tablename__ = "review_records"
    __table_args__ = (
        CheckConstraint("stage IN ('co_review','final')", name="ck_review_records_stage"),
        CheckConstraint(
            "verdict IN ('approve','reject')", name="ck_review_records_verdict"
        ),
    )

    id: Mapped[int] = _id()
    task_id: Mapped[int] = mapped_column(ForeignKey("review_tasks.id"), nullable=False)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


# ---------------------------------------------------------------------------
# 8.6 社区互动类
# ---------------------------------------------------------------------------


class Post(Base):
    """帖子：问答 / 经验长廊 / 资料求援 / 讨论 四板块（§4.2）。"""

    __tablename__ = "posts"
    __table_args__ = (
        CheckConstraint(
            "board IN ('qa','experience','bounty','discuss')", name="ck_posts_board"
        ),
        CheckConstraint(
            "status IN ('normal','featured','closed')", name="ck_posts_status"
        ),
        Index("idx_posts_board", "board", "status", text("created_at DESC")),
    )

    id: Mapped[int] = _id()
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    board: Mapped[str] = mapped_column(Text, nullable=False)
    major_id: Mapped[int | None] = mapped_column(ForeignKey("majors.id"))
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id"))
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)  # Markdown 正文
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))  # 如 {保研, 夏令营}
    bounty_score: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    ai_first_answer: Mapped[str | None] = mapped_column(Text)  # AI 首答（问答贴）
    accepted_comment_id: Mapped[int | None] = mapped_column(BigInteger)  # 采纳的评论
    view_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    status: Mapped[str] = mapped_column(Text, server_default="normal", nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = _id()
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("comments.id"))  # 楼中楼
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_accepted: Mapped[bool] = mapped_column(server_default="false", nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('post','comment','resource')", name="ck_votes_target_type"
        ),
        CheckConstraint("value IN (1,-1)", name="ck_votes_value"),
        UniqueConstraint("user_id", "target_type", "target_id"),
    )

    id: Mapped[int] = _id()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    value: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Favorite(Base):
    """收藏（含"收藏到个人库"的引用挂载）。"""

    __tablename__ = "favorites"
    __table_args__ = (
        CheckConstraint("target_type IN ('resource','post')", name="ck_favorites_target"),
        UniqueConstraint("user_id", "target_type", "target_id"),
    )

    id: Mapped[int] = _id()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Follow(Base):
    """关注课程 / 用户。"""

    __tablename__ = "follows"
    __table_args__ = (
        CheckConstraint("target_type IN ('course','user')", name="ck_follows_target"),
        UniqueConstraint("user_id", "target_type", "target_id"),
    )

    id: Mapped[int] = _id()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('resource','post','comment','user')",
            name="ck_reports_target_type",
        ),
        CheckConstraint(
            "status IN ('open','processing','resolved','dismissed')",
            name="ck_reports_status",
        ),
    )

    id: Mapped[int] = _id()
    reporter_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, server_default="open", nullable=False)
    handler_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created_at()


# ---------------------------------------------------------------------------
# 8.7 成长、通知与日志类
# ---------------------------------------------------------------------------


class ScoreLog(Base):
    """贡献分明细（事件驱动结算，事务 + 唯一约束防重复计分，模块 B4）。"""

    __tablename__ = "score_logs"
    __table_args__ = (Index("idx_score_logs_user", "user_id", text("created_at DESC")),)

    id: Mapped[int] = _id()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)  # 正负变动
    reason: Mapped[str] = mapped_column(Text, nullable=False)  # upload_approved / ...
    ref_type: Mapped[str | None] = mapped_column(Text)
    ref_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created_at()


class CreditLog(Base):
    """信用分明细（与 score_logs 结构对称；§3.3.3 要求注明裁决人与理由）。"""

    __tablename__ = "credit_logs"
    __table_args__ = (Index("idx_credit_logs_user", "user_id", text("created_at DESC")),)

    id: Mapped[int] = _id()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))  # 裁决人
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    ref_type: Mapped[str | None] = mapped_column(Text)
    ref_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created_at()


class AiQuota(Base):
    """AI 额度：按日建行；自定义 Key 启用与否由 user_llm_keys 判定（跨天一致）。"""

    __tablename__ = "ai_quotas"
    __table_args__ = (UniqueConstraint("user_id", "date"),)

    id: Mapped[int] = _id()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    used: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    daily_limit: Mapped[int] = mapped_column(Integer, server_default="30", nullable=False)
    bonus_balance: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)


class UserLlmKey(Base):
    """用户自定义模型 Key（Fernet 加密，仅后端调用时解密，前端永不回显）。"""

    __tablename__ = "user_llm_keys"

    id: Mapped[int] = _id()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False)  # 密文
    model_short: Mapped[str | None] = mapped_column(Text)
    model_medium: Mapped[str | None] = mapped_column(Text)
    model_long: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class QaLog(Base):
    """全量问答落库（模块 A5）：检索/生成指标与引用链路，供效果评估与看板。"""

    __tablename__ = "qa_logs"

    id: Mapped[int] = _id()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id"))
    search_scope: Mapped[str] = mapped_column(Text, nullable=False)  # personal/public/mixed
    coarse_chapters: Mapped[dict | None] = mapped_column(JSONB)
    top_chunks: Mapped[dict | None] = mapped_column(JSONB)
    prompt: Mapped[str | None] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    model_provider: Mapped[str | None] = mapped_column(Text)  # official / user_custom
    token_usage: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


class Notification(Base):
    """站内通知：审核结果 / 回答被采纳 / 悬赏响应 / 信用处罚 / 关注课程新资料。"""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("idx_notifications_user", "user_id", "is_read", text("created_at DESC")),
    )

    id: Mapped[int] = _id()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)  # review_result / accepted / ...
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(Text)  # 通知点击跳转落点
    is_read: Mapped[bool] = mapped_column(server_default="false", nullable=False)
    created_at: Mapped[datetime] = _created_at()


class AuditLog(Base):
    """管理操作审计：可追溯、可回滚（§3.2）。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = _id()
    admin_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)  # final_verdict / ...
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
