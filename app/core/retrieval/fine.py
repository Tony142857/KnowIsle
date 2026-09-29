"""知识点级精排（模块 A2 第二级）：粗召回范围内语义检索 + BM25（rank_bm25）双路召回。

scope / course 过滤在向量查询与 BM25 查询双层强制执行（§5.3）：
BM25 语料直接从 PG 按 course/scope/owner 过滤取出，向量查询走 Chroma where 过滤。
中文 BM25 分词用字符 bigram（不引入新依赖），ASCII 词整词保留。
"""

from rank_bm25 import BM25Okapi
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.retrieval.fusion import rrf_fuse
from app.storage.models import Chunk
from app.storage.vector_store import (
    COLLECTION_PERSONAL,
    COLLECTION_PUBLIC,
    get_vector_store,
)


def tokenize(text: str) -> list[str]:
    """中文按字符 bigram 分词，连续的字母/数字/下划线作为整词。"""
    tokens: list[str] = []
    word: list[str] = []
    prev_cjk: str | None = None

    def flush_word() -> None:
        if word:
            tokens.append("".join(word))
            word.clear()

    for ch in text:
        if "一" <= ch <= "鿿":
            flush_word()
            if prev_cjk is not None:
                tokens.append(prev_cjk + ch)
            prev_cjk = ch
        elif ch.isalnum() or ch == "_":
            word.append(ch.lower())
            prev_cjk = None
        else:
            flush_word()
            prev_cjk = None
    flush_word()
    return tokens


async def bm25_search(query: str, chunks: list[Chunk], top_k: int = 10) -> list[Chunk]:
    """在给定 chunk 行集合内做 BM25 排序（语料已由调用方按权限过滤）。"""
    if not chunks:
        return []
    bm25 = BM25Okapi([tokenize(c.content) for c in chunks])
    scores = bm25.get_scores(tokenize(query))
    order = sorted(range(len(chunks)), key=lambda i: -scores[i])[:top_k]
    return [chunks[i] for i in order if scores[i] > 0]


def _build_where(scope: str, course_id: int, owner_id: int | None,
                 chapter_ids: list[int] | None) -> dict:
    """Chroma where 过滤：course 强制；personal 再加 owner（双层过滤，§5.4）。"""
    conditions: list[dict] = [{"course_id": course_id}]
    if scope == "personal" and owner_id is not None:
        conditions.append({"owner_id": owner_id})
    if chapter_ids:
        conditions.append({"chapter_id": {"$in": list(chapter_ids)}})
    return conditions[0] if len(conditions) == 1 else {"$and": conditions}


async def semantic_search(
    session: AsyncSession,
    query_embedding: list[float],
    scope: str,
    course_id: int,
    owner_id: int | None,
    chapter_ids: list[int] | None = None,
    top_k: int = 10,
) -> list[Chunk]:
    """向量召回：按 scope 选 Collection（双库物理隔离），where 过滤后回表 PG 取行。"""
    store = get_vector_store()
    name = COLLECTION_PUBLIC if scope == "public" else COLLECTION_PERSONAL
    collection = await store.get_or_create_collection(name)
    hits = await store.query(
        collection,
        query_embedding,
        n_results=top_k,
        where=_build_where(scope, course_id, owner_id, chapter_ids),
    )
    if not hits:
        return []
    rows = (
        await session.execute(select(Chunk).where(Chunk.chunk_id.in_([h["id"] for h in hits])))
    ).scalars().all()
    by_id = {c.chunk_id: c for c in rows}
    return [by_id[h["id"]] for h in hits if h["id"] in by_id]


async def fine_search(
    session: AsyncSession,
    query: str,
    query_embedding: list[float],
    scope: str,
    course_id: int,
    owner_id: int | None,
    chapter_ids: list[int] | None = None,
    top_k: int = 5,
) -> list[dict]:
    """双路召回（语义 top10 + BM25 top10）经 RRF 融合取 top_k。

    返回 [{"chunk": Chunk 行, "score": RRF 得分}]，按得分降序。
    """
    stmt = select(Chunk).where(Chunk.course_id == course_id, Chunk.scope == scope)
    if scope == "personal":
        stmt = stmt.where(Chunk.owner_id == owner_id)
    if chapter_ids:
        stmt = stmt.where(Chunk.chapter_id.in_(chapter_ids))
    chunks = (await session.execute(stmt)).scalars().all()
    if not chunks:
        return []

    bm25_hits = await bm25_search(query, chunks, top_k=10)
    semantic_hits = await semantic_search(
        session, query_embedding, scope, course_id, owner_id, chapter_ids, top_k=10
    )
    fused = rrf_fuse(semantic_hits, bm25_hits, top_k=top_k)
    by_id = {c.chunk_id: c for c in chunks}
    return [
        {"chunk": by_id[chunk_id], "score": score}
        for chunk_id, score in fused
        if chunk_id in by_id
    ]
