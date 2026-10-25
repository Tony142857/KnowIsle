"""正文向量索引的统一写入、元数据与缓存失效约定。"""

from collections.abc import Awaitable, Callable
from typing import Any

from app.config import get_settings
from app.core.context import INDEX_VERSION, embedding_text
from app.core.retrieval.fine import invalidate_bm25
from app.storage.vector_store import COLLECTION_PERSONAL, COLLECTION_PUBLIC

EMBED_BATCH = 64


def build_chunk_meta(doc: Any, chunk: Any, embedding_model: str | None = None) -> dict:
    """Chroma 只接受标量；数据库可空字段继续使用 -1 哨兵。"""
    def value(key: str) -> Any:
        return chunk.get(key) if isinstance(chunk, dict) else getattr(chunk, key)

    return {
        "chunk_id": value("chunk_id"),
        "document_id": doc.id,
        "course_id": doc.course_id,
        "owner_id": doc.owner_id,
        "scope": doc.scope,
        "chapter_id": value("chapter_id") if value("chapter_id") is not None else -1,
        "page_num": value("page_num") if value("page_num") is not None else -1,
        "file_type": doc.file_type,
        "index_version": INDEX_VERSION,
        "embedding_model": embedding_model or get_settings().embedding_model,
    }


async def index_document_chunks(
    doc: Any,
    chunks: list[dict],
    *,
    embedding: Any,
    store: Any,
    batch_size: int = EMBED_BATCH,
) -> int:
    """按 scope 写入既有双 Collection；正文与 embedding 输入始终来自同一块。"""
    if not chunks:
        return 0
    name = COLLECTION_PUBLIC if doc.scope == "public" else COLLECTION_PERSONAL
    collection = await store.get_or_create_collection(name)
    model = get_settings().embedding_model
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        vectors = await embedding.embed([embedding_text(chunk) for chunk in batch])
        await store.upsert(
            collection,
            ids=[chunk["chunk_id"] for chunk in batch],
            embeddings=vectors,
            metadatas=[build_chunk_meta(doc, chunk, model) for chunk in batch],
            documents=[chunk["content"] for chunk in batch],
        )
    return len(chunks)


async def invalidate_document_index(
    doc: Any,
    invalidate: Callable[[int, str, int | None], Awaitable[None]] = invalidate_bm25,
) -> None:
    """所有 chunks 写入点在 PG 提交后使用同一权限键失效 BM25。"""
    await invalidate(doc.course_id, doc.scope, doc.owner_id)
