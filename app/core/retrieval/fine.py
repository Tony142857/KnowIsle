"""知识点级精排（模块 A2 第二级）：粗召回范围内语义检索 + BM25（rank_bm25）双路召回。

scope / course 过滤在向量查询与 BM25 查询双层强制执行（§5.3）：
BM25 语料直接从 PG 按 course/scope/owner 过滤取出，向量查询走 Chroma where 过滤。
中文 BM25 分词用字符 bigram（不引入新依赖），ASCII 词整词保留。

v0.9 性能优化（B1）：BM25 语料按 (course_id, scope, owner_id) 建进程内缓存
（分词 + BM25Okapi 实例 + 语料 id 列表），不再每次问答全量捞出逐条分词重建；
构建与打分均为 CPU 密集操作，经 asyncio.to_thread 挪出事件循环。
缓存有界：最多 32 项 LRU + 600s TTL 兜底，语料变更点（解析落切块 / 终审派生
public 副本 / 克隆入个人库）调用 invalidate_bm25 精准失效。
"""

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field

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


# ---------------------------------------------------------------------------
# BM25 语料缓存（v0.9）
# ---------------------------------------------------------------------------

_BM25_CACHE_MAX = 32  # 最多缓存 32 个 (课程, scope, owner) 语料，超出按 LRU 逐出
_BM25_CACHE_TTL = 600.0  # 秒，兜底过期（正常靠写入点 invalidate_bm25 精准失效）


@dataclass
class _Bm25Entry:
    """单语料的缓存项：BM25 实例 + 分词结果 + 与语料对齐的 id 列表。"""

    bm25: BM25Okapi
    tokens: list[list[str]]
    chunk_ids: list[str]
    chapter_ids: list[int | None]
    built_at: float = field(default_factory=time.monotonic)


_bm25_cache: OrderedDict[tuple[int, str, int | None], _Bm25Entry] = OrderedDict()


def _bm25_key(course_id: int, scope: str, owner_id: int | None) -> tuple[int, str, int | None]:
    # public 语料不按 owner 过滤，owner 归一为 None，避免同一课程刷出多份缓存
    return (course_id, scope, owner_id if scope == "personal" else None)


def invalidate_bm25(course_id: int, scope: str, owner_id: int | None = None) -> None:
    """语料变更（解析落切块 / 终审派生 public 副本 / 克隆入个人库）后调用，
    下次检索重建该 (course_id, scope, owner_id) 的 BM25 索引。"""
    _bm25_cache.pop(_bm25_key(course_id, scope, owner_id), None)


def _build_bm25_entry(rows: list) -> _Bm25Entry:
    """由 (chunk_id, chapter_id, content) 行构建缓存项（CPU 密集，调用方经 to_thread 执行）。"""
    chunk_ids = [r[0] for r in rows]
    tokens = [tokenize(r[2]) for r in rows]
    return _Bm25Entry(
        bm25=BM25Okapi(tokens),
        tokens=tokens,
        chunk_ids=chunk_ids,
        chapter_ids=[r[1] for r in rows],
    )


def _bm25_top_ids(
    entry: _Bm25Entry, query: str, chapter_ids: list[int] | None, top_k: int
) -> list[str]:
    """在缓存语料上打分取 top（CPU 密集，调用方经 to_thread 执行）。"""
    scores = entry.bm25.get_scores(tokenize(query))
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    chapter_set = set(chapter_ids) if chapter_ids else None
    hits: list[str] = []
    for i in order:
        if scores[i] <= 0:
            break
        if chapter_set is not None and entry.chapter_ids[i] not in chapter_set:
            continue
        hits.append(entry.chunk_ids[i])
        if len(hits) >= top_k:
            break
    return hits


async def _get_bm25_entry(
    session: AsyncSession, course_id: int, scope: str, owner_id: int | None
) -> _Bm25Entry | None:
    """取该 (course_id, scope, owner_id) 的 BM25 缓存项，缺失/过期则查库重建；
    语料为空返回 None。"""
    key = _bm25_key(course_id, scope, owner_id)
    entry = _bm25_cache.get(key)
    if entry is not None and time.monotonic() - entry.built_at >= _BM25_CACHE_TTL:
        entry = None
        _bm25_cache.pop(key, None)
    if entry is not None:
        _bm25_cache.move_to_end(key)
        return entry

    stmt = select(Chunk.chunk_id, Chunk.chapter_id, Chunk.content).where(
        Chunk.course_id == course_id, Chunk.scope == scope
    )
    if scope == "personal":
        stmt = stmt.where(Chunk.owner_id == owner_id)
    rows = (await session.execute(stmt)).all()
    if not rows:
        return None
    entry = await asyncio.to_thread(_build_bm25_entry, rows)
    _bm25_cache[key] = entry
    _bm25_cache.move_to_end(key)
    while len(_bm25_cache) > _BM25_CACHE_MAX:
        _bm25_cache.popitem(last=False)
    return entry


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
    BM25 语料走 (course_id, scope, owner_id) 进程内缓存（见模块头注）；
    语义命中再按缓存语料集合做一次 PG 侧归属校验（双库过滤的第二层）。
    """
    entry = await _get_bm25_entry(session, course_id, scope, owner_id)
    if entry is None:
        return []

    semantic_hits = await semantic_search(
        session, query_embedding, scope, course_id, owner_id, chapter_ids, top_k=10
    )
    # 缓存语料集合即权限边界（course/scope/owner 已过滤）；chapter 过滤在两侧各自施加
    allowed = set(entry.chunk_ids)
    if chapter_ids:
        chapter_set = set(chapter_ids)
        allowed = {
            cid
            for cid, ch in zip(entry.chunk_ids, entry.chapter_ids, strict=True)
            if ch in chapter_set
        }
    semantic_hits = [c for c in semantic_hits if c.chunk_id in allowed]

    bm25_ids = await asyncio.to_thread(_bm25_top_ids, entry, query, chapter_ids, 10)
    bm25_hits: list[Chunk] = []
    if bm25_ids:
        rows = (
            await session.execute(select(Chunk).where(Chunk.chunk_id.in_(bm25_ids)))
        ).scalars().all()
        by_id = {c.chunk_id: c for c in rows}
        bm25_hits = [by_id[cid] for cid in bm25_ids if cid in by_id]

    fused = rrf_fuse(semantic_hits, bm25_hits, top_k=top_k)
    by_id = {c.chunk_id: c for c in [*semantic_hits, *bm25_hits]}
    return [
        {"chunk": by_id[chunk_id], "score": score}
        for chunk_id, score in fused
        if chunk_id in by_id
    ]
