"""目录树构建（模块 A1）：自动解析 + AI 校验 + 共建者/管理员人工修正。

输出「章节 → 小节」目录树，节点唯一 ID，写入 chapters 表。
v0.3：从 ParsedBlock 提取 level=1 标题作为章节（按 course_id+title 去重复用已有章节，
order_idx 递增）；level=2 标题由切块器填入 chunk.section；无一级标题时章节为 None
（chunks.chapter_id 允许 NULL，§8.3）。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.parser.base import ParsedBlock
from app.storage.models import Chapter


async def build_tree(
    session: AsyncSession, blocks: list[ParsedBlock], course_id: int
) -> list[int | None]:
    """为 blocks 中的 level=1 标题建/复用章节，返回与 blocks 等长的「块序 → chapter_id」映射。

    只 add + flush（拿到自增 ID），事务提交由调用方（解析流水线）统一负责。
    """
    existing = (
        await session.execute(select(Chapter).where(Chapter.course_id == course_id))
    ).scalars().all()
    by_title = {c.title: c for c in existing if c.parent_id is None}
    next_order = (max((c.order_idx for c in existing), default=-1)) + 1

    mapping: list[int | None] = []
    current: Chapter | None = None
    for block in blocks:
        if block.level == 1 and block.title:
            chapter = by_title.get(block.title)
            if chapter is None:
                chapter = Chapter(
                    course_id=course_id,
                    title=block.title,
                    parent_id=None,
                    order_idx=next_order,
                )
                next_order += 1
                session.add(chapter)
                await session.flush()
                by_title[block.title] = chapter
            current = chapter
        mapping.append(current.id if current else None)
    return mapping
