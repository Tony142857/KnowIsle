"""章节摘要回填任务（模块 A2 配套）：公共课程章节摘要向量，供章节级粗召回使用。

摘要向量与正文向量分离存储于 chapter_summaries 集合（集合约定见
core/retrieval/coarse.py）：id 即 chapters.summary_vector_id（ch_{chapter_id}），
metadata 带 course_id 供过滤。终审上架新资料后由管理接口入队，
对目标课程全量回填（upsert 幂等覆盖）。
"""

import logging

from sqlalchemy import select

from app.core.embeddings import get_embedding
from app.core.llm.prompts import CHAPTER_SUMMARY_PROMPT
from app.core.llm.router import ModelTier, get_official_client
from app.core.retrieval.coarse import COLLECTION_SUMMARIES
from app.storage.db import SessionLocal
from app.storage.models import Chapter, Chunk
from app.storage.vector_store import get_vector_store

logger = logging.getLogger(__name__)

_CHAPTER_TEXT_LIMIT = 4000  # 单章摘要输入正文长度上限


async def backfill_chapter_summaries(ctx: dict, course_id: int) -> None:
    """ARQ 任务：逐章聚合 public chunks → MEDIUM 档 LLM 摘要 → embed →
    chapter_summaries 集合 upsert → 回填 chapters.summary_vector_id。
    章节无 public 内容则跳过；单章 LLM 失败记 warning 跳过继续，不影响其余章节。"""
    async with SessionLocal() as session:
        chapters = (
            await session.execute(
                select(Chapter).where(Chapter.course_id == course_id).order_by(Chapter.id)
            )
        ).scalars().all()
        store = get_vector_store()
        collection = await store.get_or_create_collection(COLLECTION_SUMMARIES)
        embedding = get_embedding()
        client = get_official_client(ModelTier.MEDIUM)
        for chapter in chapters:
            chunks = (
                await session.execute(
                    select(Chunk)
                    .where(
                        Chunk.course_id == course_id,
                        Chunk.scope == "public",
                        Chunk.chapter_id == chapter.id,
                    )
                    .order_by(Chunk.id)
                )
            ).scalars().all()
            text = "\n".join(c.content for c in chunks)[:_CHAPTER_TEXT_LIMIT]
            if not text.strip():
                continue
            vector_id = f"ch_{chapter.id}"
            try:
                summary, _ = await client.chat(
                    [{"role": "user", "content": CHAPTER_SUMMARY_PROMPT.format(chapter_text=text)}]
                )
                vectors = await embedding.embed([summary])
                await store.upsert(
                    collection,
                    ids=[vector_id],
                    embeddings=vectors,
                    metadatas=[{"course_id": course_id}],
                    documents=[summary],
                )
                chapter.summary_vector_id = vector_id
            except Exception:
                logger.warning(
                    "章节摘要回填失败（跳过）chapter_id=%s", chapter.id, exc_info=True
                )
                continue
        await session.commit()
    logger.info("章节摘要回填完成 course_id=%s", course_id)
