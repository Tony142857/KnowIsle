"""帖子服务（模块 B3）：Markdown + 图片，板块 qa / experience / bounty / discuss。

问答贴发帖后触发 ARQ 异步任务生成 AI 首答（基于本课程公共库，带引用溯源，
标注"AI 生成，仅供参考"），写入 posts.ai_first_answer 并通知提问者。
v0.7：经验长廊（结构化模板 + AI 摘要异步生成）与资料求援（悬赏托管/采纳结算）开放。
"""

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pipeline import extract_citations
from app.storage.models import Chapter, Chunk, Document, Post, User

OPEN_BOARDS = ("qa", "discuss", "experience", "bounty")  # v0.7 四板块全部开放

TAG_MAX_COUNT = 5
TAG_MAX_LENGTH = 20
EXCERPT_LENGTH = 120

AI_ANSWER_KEY_TTL = 24 * 3600  # AI 首答状态键过期时间（秒）
SUMMARY_KEY_TTL = 24 * 3600  # 经验帖 AI 摘要状态键过期时间（秒）

# 与 core/pipeline.py 的 chunk_id 引用格式一致（[per_d12_00034] 式）
_CITATION_RE = re.compile(r"\[((?:per|pub)_d\d+_\d{4,})\]")


def ai_answer_key(post_id: int) -> str:
    """AI 首答状态 Redis 键（pending / failed，生成成功后删除）。"""
    return f"knowisle:ai_answer:{post_id}"


def exp_summary_key(post_id: int) -> str:
    """经验帖 AI 摘要状态 Redis 键（pending / failed，生成成功后删除）。"""
    return f"knowisle:exp_summary:{post_id}"


def validate_board(board: str) -> None:
    """板块白名单校验（纯函数）：qa / discuss / experience / bounty 四板块开放。"""
    if board not in OPEN_BOARDS:
        raise ValueError("未知板块")


def validate_tags(tags: list[str] | None) -> list[str]:
    """标签校验（纯函数）：≤5 个、每个 ≤20 字符，去重并保持顺序。"""
    if not tags:
        return []
    if len(tags) > TAG_MAX_COUNT:
        raise ValueError(f"标签最多 {TAG_MAX_COUNT} 个")
    for tag in tags:
        if not tag or len(tag) > TAG_MAX_LENGTH:
            raise ValueError(f"标签长度须为 1-{TAG_MAX_LENGTH} 字符")
    return list(dict.fromkeys(tags))


def make_excerpt(content: str) -> str:
    """列表摘要（纯函数）：剥离 Markdown 标记后取前 120 字（v1.0：经验帖四节模板
    的 `## 小节` 标记不再出现在板块列表摘要里）。"""
    text = re.sub(r"^#{1,6}\s*", "", content, flags=re.MULTILINE)  # 标题标记
    text = re.sub(r"[*_`>#]", "", text)  # 强调/代码/引用/残留井号
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # 链接保留文字
    text = re.sub(r"\s+", " ", text).strip()
    return text[:EXCERPT_LENGTH]


def derive_ai_answer_status(
    board: str, ai_first_answer: str | None, redis_state: str | None
) -> str:
    """AI 首答状态推导（纯函数）：done > Redis pending/failed > none；非 qa 帖恒为 none。"""
    if board != "qa":
        return "none"
    if ai_first_answer:
        return "done"
    if redis_state in ("pending", "failed"):
        return redis_state
    return "none"


def derive_summary_status(board: str, ai_summary: str | None, redis_state: str | None) -> str:
    """经验帖 AI 摘要状态推导（纯函数）：done > Redis pending/failed > none；非经验帖恒为 none。"""
    if board != "experience":
        return "none"
    if ai_summary:
        return "done"
    if redis_state in ("pending", "failed"):
        return redis_state
    return "none"


async def build_ai_citations(
    session: AsyncSession, answer: str, post: Post | None = None
) -> list[dict]:
    """仅渲染当次证据集合；旧帖回退也必须匹配该帖课程与 public scope。

    既有路由只传 answer，因此先反查唯一帖子；相同正文对应多帖时拒绝猜测。
    """
    candidates = set(_CITATION_RE.findall(answer))
    if not candidates:
        return []
    if post is None:
        posts = list(
            (
                await session.execute(select(Post).where(Post.ai_first_answer == answer))
            ).scalars().all()
        )
        if len(posts) != 1:
            return []
        post = posts[0]
    allowed = getattr(post, "ai_citation_chunk_ids", None)
    cited_ids = extract_citations(
        answer, candidates if allowed is None else set(allowed) & candidates
    )
    if not cited_ids:
        return []
    rows = (
        await session.execute(
            select(Chunk, Document, User.nickname, Chapter.title)
            .join(Document, Document.id == Chunk.document_id)
            .join(User, User.id == Chunk.owner_id)
            .outerjoin(Chapter, Chapter.id == Chunk.chapter_id)
            .where(
                Chunk.chunk_id.in_(cited_ids),
                Chunk.course_id == post.course_id,
                Chunk.scope == "public",
            )
        )
    ).all()
    info = {
        chunk.chunk_id: (chunk, doc, nickname, chapter_title)
        for chunk, doc, nickname, chapter_title in rows
        if chunk.course_id == post.course_id and chunk.scope == "public"
    }
    items = []
    for chunk_id in cited_ids:
        entry = info.get(chunk_id)
        if entry is None:
            continue
        chunk, doc, nickname, chapter_title = entry
        items.append(
            {
                "chunk_id": chunk.chunk_id,
                "document_id": doc.id,
                "chapter": chapter_title,
                "section": chunk.section,
                "page_num": chunk.page_num,
                "uploader": nickname,
            }
        )
    return items
