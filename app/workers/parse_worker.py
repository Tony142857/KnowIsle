"""解析与向量化任务（模块 A1）：解析 → 目录树 → 结构规则切块 → 向量化 → 落库 → AI 校验。

上传即返回，页面轮询 /api/documents/{id}/status 更新进度；
失败任务自动重试 3 次后标记 failed 并记录安全原因（arq max_tries=3，见 workers/settings.py）。
chunk_id 与章节复用均幂等：重试/重解析先清旧切块再插入，不留半成品。

Office→PDF 预览转换（soffice）与 preview_key 回填见 workers/preview_worker（v0.4）。
"""

import logging

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.chunking import semantic_splitter
from app.core.embeddings import get_embedding
from app.core.indexing import EMBED_BATCH as _EMBED_BATCH
from app.core.indexing import build_chunk_meta as _build_chunk_meta
from app.core.indexing import index_document_chunks, invalidate_document_index
from app.core.parser import parse_by_file_type
from app.core.retrieval.fine import invalidate_bm25
from app.core.structure import ai_verifier, tree_builder
from app.storage import object_store
from app.storage.db import SessionLocal
from app.storage.models import Chunk, Document
from app.storage.vector_store import get_vector_store

logger = logging.getLogger(__name__)


def build_chunk_meta(doc: Document, chunk: dict) -> dict:
    return _build_chunk_meta(doc, chunk)


def public_parse_error(exc: Exception) -> str:
    """把内部解析异常收敛为可公开、无堆栈和路径信息的用户文案。"""
    if isinstance(exc, ValueError) and (
        "解析结果为空" in str(exc) or "切块结果为空" in str(exc)
    ):
        return "文档中未提取到可用文本，请检查文件内容或更换格式后重试"
    if isinstance(exc, (UnicodeError, ValueError)):
        return "文档内容或格式无法解析，请确认文件未损坏后重试"
    return "文档解析失败，请稍后重试；若持续失败，请更换文件格式"


async def _run_pipeline(session: AsyncSession, doc: Document) -> list:
    doc.parse_error = None
    data = await object_store.get_object(doc.storage_key)
    blocks = parse_by_file_type(doc.file_type, data)
    if not blocks:
        raise ValueError(f"文档解析结果为空: document_id={doc.id}")

    chapter_ids = await tree_builder.build_tree(session, blocks, doc.course_id)
    chunks = semantic_splitter.split_blocks(blocks, doc.id, doc.scope, chapter_ids)
    if not chunks:
        raise ValueError(f"文档切块结果为空: document_id={doc.id}")

    await index_document_chunks(
        doc,
        chunks,
        embedding=get_embedding(),
        store=get_vector_store(),
        batch_size=_EMBED_BATCH,
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
    # chunks 已落库：失效该课程 BM25 语料缓存（v0.9；v1.1 起含 Redis 版本戳跨进程失效）
    await invalidate_document_index(doc, invalidate_bm25)
    return blocks


async def parse_document(ctx: dict, document_id: int) -> None:
    """ARQ 任务：对象存储取件 → parser 解析 → tree_builder → semantic_splitter
    → embeddings → Chroma 写入（按 scope 分 Collection）→ PG 落切块；
    成功后附加章节树 AI 校验（仅记日志，结果不影响解析状态）。"""
    async with SessionLocal() as session:
        doc = await session.get(Document, document_id)
        if doc is None:
            logger.warning("解析任务跳过：文档不存在 document_id=%s", document_id)
            return
        try:
            blocks = await _run_pipeline(session, doc)
        except Exception as exc:
            await session.rollback()
            if ctx.get("job_try", 1) >= 3:
                # 只暴露安全文案；原始异常仍由 ARQ/日志记录，不写入用户状态。
                doc.status = "failed"
                doc.parse_error = public_parse_error(exc)
                await session.commit()
            raise
    # 章节树 AI 校验（模块 A1 附加层）：解析主流程成功后才执行；verify_tree_safe
    # 内部对 LLM 异常/超时/解析失败一律降级为规则结果并记日志，绝不抛出，
    # 不影响 doc.status 与切块落库结论（结论仅写日志，见 ai_verifier 模块 docstring）。
    await ai_verifier.verify_tree_safe(blocks, document_id=doc.id)
    logger.info("文档解析完成 document_id=%s", document_id)
