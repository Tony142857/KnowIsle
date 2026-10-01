"""解析与向量化任务（模块 A1）：解析 → 目录树 → 语义切块 → 向量化 → 落库。

上传即返回，页面轮询 /api/documents/{id}/status 更新进度；
失败任务自动重试 3 次后标记 failed 并通知上传者（arq max_tries=3，见 workers/settings.py）。
chunk_id 与章节复用均幂等：重试/重解析先清旧切块再插入，不留半成品。

Office→PDF 预览转换（soffice）与 preview_key 回填见 workers/preview_worker（v0.4）。
"""

import logging

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.chunking import semantic_splitter
from app.core.embeddings import get_embedding
from app.core.parser import parse_by_file_type
from app.core.retrieval.fine import invalidate_bm25
from app.core.structure import tree_builder
from app.storage import object_store
from app.storage.db import SessionLocal
from app.storage.models import Chunk, Document
from app.storage.vector_store import (
    COLLECTION_PERSONAL,
    COLLECTION_PUBLIC,
    get_vector_store,
)

logger = logging.getLogger(__name__)

_EMBED_BATCH = 64  # 向量化批大小


def build_chunk_meta(doc: Document, chunk: dict) -> dict:
    """Chroma metadata（值仅 str/int/float/bool；可空字段用 -1 哨兵）。"""
    return {
        "chunk_id": chunk["chunk_id"],
        "document_id": doc.id,
        "course_id": doc.course_id,
        "owner_id": doc.owner_id,
        "chapter_id": chunk["chapter_id"] if chunk["chapter_id"] is not None else -1,
        "page_num": chunk["page_num"] if chunk["page_num"] is not None else -1,
        "file_type": doc.file_type,
    }


async def _run_pipeline(session: AsyncSession, doc: Document) -> None:
    data = await object_store.get_object(doc.storage_key)
    blocks = parse_by_file_type(doc.file_type, data)
    if not blocks:
        raise ValueError(f"文档解析结果为空: document_id={doc.id}")

    chapter_ids = await tree_builder.build_tree(session, blocks, doc.course_id)
    chunks = semantic_splitter.split_blocks(blocks, doc.id, doc.scope, chapter_ids)
    if not chunks:
        raise ValueError(f"文档切块结果为空: document_id={doc.id}")

    embedding = get_embedding()
    vectors: list[list[float]] = []
    for i in range(0, len(chunks), _EMBED_BATCH):
        vectors.extend(await embedding.embed([c["content"] for c in chunks[i : i + _EMBED_BATCH]]))

    store = get_vector_store()
    name = COLLECTION_PUBLIC if doc.scope == "public" else COLLECTION_PERSONAL
    collection = await store.get_or_create_collection(name)
    await store.upsert(
        collection,
        ids=[c["chunk_id"] for c in chunks],
        embeddings=vectors,
        metadatas=[build_chunk_meta(doc, c) for c in chunks],
        documents=[c["content"] for c in chunks],
    )

    # 幂等：先清旧切块再插入（重试/重解析不留半成品）
    await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    session.add_all(
        Chunk(
            chunk_id=c["chunk_id"],
            document_id=doc.id,
            course_id=doc.course_id,
            chapter_id=c["chapter_id"],
            owner_id=doc.owner_id,
            scope=doc.scope,
            section=c["section"],
            page_num=c["page_num"],
            file_type=doc.file_type,
            content=c["content"],
            embedding_id=c["chunk_id"],
        )
        for c in chunks
    )
    doc.status = "parsed"
    await session.commit()
    # chunks 已落库：失效该课程 BM25 语料缓存（v0.9），下次检索重建
    invalidate_bm25(doc.course_id, doc.scope, doc.owner_id)


async def parse_document(ctx: dict, document_id: int) -> None:
    """ARQ 任务：对象存储取件 → parser 解析 → tree_builder → semantic_splitter
    → embeddings → Chroma 写入（按 scope 分 Collection）→ PG 落切块。"""
    async with SessionLocal() as session:
        doc = await session.get(Document, document_id)
        if doc is None:
            logger.warning("解析任务跳过：文档不存在 document_id=%s", document_id)
            return
        try:
            await _run_pipeline(session, doc)
        except Exception:
            await session.rollback()
            if ctx.get("job_try", 1) >= 3:
                # 最后一次重试仍失败：标记 failed 并提交后再抛出（交 arq 终结任务）
                doc.status = "failed"
                await session.commit()
            raise
    logger.info("文档解析完成 document_id=%s", document_id)
