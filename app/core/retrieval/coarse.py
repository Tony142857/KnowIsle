"""章节级粗召回（模块 A2 第一级）。

章节摘要向量相似度召回 Top 3-5 章节，把范围从整门课缩小到几章；
章节摘要为空（summary_vector_id 未回填）时返回 None，
调用方降级为全课程范围精排（§8.3 既定降级路径）。

章节摘要向量与正文向量分离存储于 chapter_summaries 集合，
id 即 chapters.summary_vector_id，metadata 带 course_id 供过滤。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import Chapter
from app.storage.vector_store import get_vector_store

COLLECTION_SUMMARIES = "chapter_summaries"


async def coarse_recall(
    session: AsyncSession,
    query_embedding: list[float],
    course_id: int,
    scope: str,
    top_k: int = 5,
) -> list[int] | None:
    """返回粗召回的章节 ID 列表；无摘要向量可用时返回 None（降级全课程精排）。"""
    chapters = (
        await session.execute(
            select(Chapter).where(
                Chapter.course_id == course_id, Chapter.summary_vector_id.isnot(None)
            )
        )
    ).scalars().all()
    if not chapters:
        return None
    store = get_vector_store()
    collection = await store.get_or_create_collection(COLLECTION_SUMMARIES)
    hits = await store.query(
        collection,
        query_embedding,
        n_results=top_k,
        where={"course_id": course_id},
    )
    vector_to_chapter = {c.summary_vector_id: c.id for c in chapters}
    chapter_ids = [vector_to_chapter[h["id"]] for h in hits if h["id"] in vector_to_chapter]
    return chapter_ids or None
