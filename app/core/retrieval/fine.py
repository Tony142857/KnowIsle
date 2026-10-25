"""知识点级精排（模块 A2 第二级）：粗召回范围内语义检索 + BM25（rank_bm25）双路召回。

scope / course 过滤在向量查询与 BM25 查询双层强制执行（§5.3）：
BM25 语料直接从 PG 按 course/scope/owner 过滤取出，向量查询走 Chroma where 过滤。
中文 BM25 分词用字符 bigram（不引入新依赖），ASCII 词整词保留。

v0.9 性能优化（B1）：BM25 语料按 (course_id, scope, owner_id) 建进程内缓存
（分词 + BM25Okapi 实例 + 语料 id 列表），不再每次问答全量捞出逐条分词重建；
构建与打分均为 CPU 密集操作，经 asyncio.to_thread 挪出事件循环。
缓存有界：最多 32 项 LRU + 600s TTL 兜底，语料变更点（解析落切块 / 终审派生
public 副本 / 克隆入个人库）调用 invalidate_bm25 精准失效。

§20.5 B2（v1.1）：进程内缓存命中前比对 Redis 版本戳（knowisle:bm25ver:*，
invalidate 时 incr），worker 与 web 分进程也能跨进程失效；Redis 读失败时
按现状仅用 TTL，优雅降级。
"""

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from rank_bm25 import BM25Okapi
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.context import INDEX_VERSION
from app.core.retrieval.evidence import (
    filter_sufficient_evidence,
    has_lexical_overlap,
    lexical_overlap_count,
    needs_global_fallback,
    tokenize,
)
from app.core.retrieval.fusion import rrf_fuse
from app.storage.cache import get_redis
from app.storage.models import Chunk
from app.storage.vector_store import (
    COLLECTION_PERSONAL,
    COLLECTION_PUBLIC,
    get_vector_store,
)

logger = logging.getLogger(__name__)


_has_lexical_overlap = has_lexical_overlap


class SearchResults(list):
    """兼容既有 list 返回值，并携带不进入 SSE 契约的检索诊断。"""

    def __init__(self, values=(), *, metrics: dict | None = None):
        super().__init__(values)
        self.metrics = metrics or {}


async def bm25_search(query: str, chunks: list[Chunk], top_k: int = 10) -> list[Chunk]:
    """在给定 chunk 行集合内做 BM25 排序（语料已由调用方按权限过滤）。"""
    if not chunks:
        return []
    tokens = [tokenize(chunk.content) for chunk in chunks]
    query_tokens = set(tokenize(query))
    bm25 = BM25Okapi(tokens)
    scores = bm25.get_scores(tokenize(query))
    if max(scores, default=0.0) <= 0:
        # 小语料高频词的 IDF 可能全为负：改用词面命中数排序，避免把
        # 全量负分候选当作文档证据（v1.3 实测修正）。
        overlaps = [
            lexical_overlap_count(query_tokens, chunk_tokens) for chunk_tokens in tokens
        ]
        order = sorted(
            range(len(chunks)),
            key=lambda i: (-overlaps[i], chunks[i].chunk_id),
        )
        hits = [chunks[i] for i in order if overlaps[i] > 0]
    else:
        order = sorted(range(len(chunks)), key=lambda i: -scores[i])
        hits = [
            chunks[i] for i in order
            if _has_lexical_overlap(query_tokens, tokens[i])
        ]
    return hits[:top_k]


# ---------------------------------------------------------------------------
# BM25 语料缓存（v0.9）
# ---------------------------------------------------------------------------

_BM25_CACHE_MAX = 32  # 最多缓存 32 个 (课程, scope, owner) 语料，超出按 LRU 逐出
_BM25_CACHE_TTL = 600.0  # 秒，兜底过期（正常靠写入点 invalidate_bm25 精准失效）


@dataclass
class _Bm25Entry:
    """单语料的缓存项：BM25 实例 + 分词结果 + 与语料对齐的 id 列表。

    version 为构建时读到的 Redis 版本戳（§20.5 B2）；None 表示 Redis 不可用，
    命中比对时跳过版本校验（仅靠 TTL 兜底）。
    """

    bm25: BM25Okapi
    tokens: list[list[str]]
    chunk_ids: list[str]
    chapter_ids: list[int | None]
    built_at: float = field(default_factory=time.monotonic)
    version: int | None = None


_bm25_cache: OrderedDict[tuple[int, str, int | None], _Bm25Entry] = OrderedDict()


def _bm25_key(course_id: int, scope: str, owner_id: int | None) -> tuple[int, str, int | None]:
    # public 语料不按 owner 过滤，owner 归一为 None，避免同一课程刷出多份缓存
    return (course_id, scope, owner_id if scope == "personal" else None)


def _bm25_version_key(key: tuple[int, str, int | None]) -> str:
    """语料版本戳 Redis 键（§20.5 B2）：knowisle:bm25ver:{course}:{scope}:{owner}。"""
    course_id, scope, owner_id = key
    return f"knowisle:bm25ver:{course_id}:{scope}:{owner_id or 0}"


async def _corpus_version(key: tuple[int, str, int | None]) -> int | None:
    """读语料版本戳；Redis 读失败返回 None（优雅降级：命中仅靠 TTL，§20.5 B2）。"""
    try:
        raw = await get_redis().get(_bm25_version_key(key))
    except Exception:
        logger.warning("BM25 版本戳读取失败（按 TTL 兜底）key=%s", key, exc_info=True)
        return None
    return int(raw) if raw is not None else 0


async def invalidate_bm25(course_id: int, scope: str, owner_id: int | None = None) -> None:
    """语料变更（解析落切块 / 终审派生 public 副本 / 克隆入个人库）后调用：
    incr Redis 版本戳（跨进程失效，§20.5 B2）+ 清本进程缓存。
    Redis 写失败仅记日志——本进程缓存已清，其它进程靠 TTL 兜底。"""
    key = _bm25_key(course_id, scope, owner_id)
    _bm25_cache.pop(key, None)
    try:
        await get_redis().incr(_bm25_version_key(key))
    except Exception:
        logger.warning("BM25 版本戳递增失败（其它进程靠 TTL 兜底）key=%s", key, exc_info=True)


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
    query_tokens = set(tokenize(query))
    scores = entry.bm25.get_scores(tokenize(query))
    if max(scores, default=0.0) <= 0:
        # IDF 全为负时改用词面命中数，避免全量负分候选污染融合。
        overlaps = [
            lexical_overlap_count(query_tokens, tokens) for tokens in entry.tokens
        ]
        order = sorted(range(len(scores)), key=lambda i: (-overlaps[i], entry.chunk_ids[i]))
        min_overlap = 1
    else:
        order = sorted(range(len(scores)), key=lambda i: -scores[i])
        min_overlap = -1
    chapter_set = set(chapter_ids) if chapter_ids else None
    hits: list[str] = []
    for i in order:
        if min_overlap >= 0:
            if overlaps[i] < min_overlap:
                continue
        elif not _has_lexical_overlap(query_tokens, entry.tokens[i]):
            continue
        if chapter_set is not None and entry.chapter_ids[i] not in chapter_set:
            continue
        hits.append(entry.chunk_ids[i])
        if len(hits) >= top_k:
            break
    return hits


async def _get_bm25_entry(
    session: AsyncSession, course_id: int, scope: str, owner_id: int | None
) -> _Bm25Entry | None:
    """取该 (course_id, scope, owner_id) 的 BM25 缓存项，缺失/过期/版本戳落后
    （其它进程已写 chunks，§20.5 B2）则查库重建；语料为空返回 None。"""
    key = _bm25_key(course_id, scope, owner_id)
    entry = _bm25_cache.get(key)
    if entry is not None and time.monotonic() - entry.built_at >= _BM25_CACHE_TTL:
        entry = None
        _bm25_cache.pop(key, None)
    if entry is not None:
        current = await _corpus_version(key)
        if current is None or current == entry.version:
            _bm25_cache.move_to_end(key)
            return entry
        entry = None  # 版本戳落后：其它进程已变更语料，丢弃本进程缓存重建
        _bm25_cache.pop(key, None)

    # 先读版本戳再查语料：构建中途的 invalidate 只会让缓存版本偏旧（下次命中
    # 比对 mismatch 再重建一次），不会把「新版本戳 + 旧语料」的脏项留在缓存里
    version = await _corpus_version(key)
    stmt = select(Chunk.chunk_id, Chunk.chapter_id, Chunk.content).where(
        Chunk.course_id == course_id, Chunk.scope == scope
    )
    if scope == "personal":
        stmt = stmt.where(Chunk.owner_id == owner_id)
    rows = (await session.execute(stmt)).all()
    if not rows:
        return None
    entry = await asyncio.to_thread(_build_bm25_entry, rows)
    entry.version = version
    _bm25_cache[key] = entry
    _bm25_cache.move_to_end(key)
    while len(_bm25_cache) > _BM25_CACHE_MAX:
        _bm25_cache.popitem(last=False)
    return entry


def _build_where(scope: str, course_id: int, owner_id: int | None,
                 chapter_ids: list[int] | None) -> dict:
    """Chroma where 过滤：course 强制；personal 再加 owner（双层过滤，§5.4）。"""
    conditions: list[dict] = [{"course_id": course_id}]
    if scope == "personal":
        conditions.append({"owner_id": owner_id if owner_id is not None else -1})
    if chapter_ids:
        conditions.append({"chapter_id": {"$in": list(chapter_ids)}})
    return conditions[0] if len(conditions) == 1 else {"$and": conditions}


async def _index_info(scope: str) -> dict:
    name = COLLECTION_PUBLIC if scope == "public" else COLLECTION_PERSONAL
    try:
        metadata = await get_vector_store().get_collection_metadata(name)
    except Exception:
        logger.warning("Collection 元数据读取失败 collection=%s", name, exc_info=True)
        metadata = {}
    state = metadata.get("state", "ready")
    return {
        "collection": name,
        "index_version": metadata.get("index_version"),
        "embedding_model": metadata.get("embedding_model"),
        "index_state": state,
        "semantic_enabled": state == "ready",
    }


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
    result = []
    for hit in hits:
        chunk = by_id.get(hit["id"])
        if chunk is not None:
            metadata = hit.get("metadata") or {}
            chunk._rag_distance = hit.get("distance")
            chunk._rag_index_version = metadata.get("index_version")
            chunk._rag_embedding_model = metadata.get("embedding_model")
            result.append(chunk)
    return result


async def _permission_chunks(
    session: AsyncSession,
    course_id: int,
    scope: str,
    owner_id: int | None,
    chapter_ids: list[int] | None,
) -> list[Chunk]:
    """BM25 缓存故障时仍从 PG 重建权限边界，绝不放宽 owner/scope/course。"""
    if scope == "personal" and owner_id is None:
        return []
    stmt = select(Chunk).where(Chunk.course_id == course_id, Chunk.scope == scope)
    if scope == "personal":
        stmt = stmt.where(Chunk.owner_id == owner_id)
    if chapter_ids:
        stmt = stmt.where(Chunk.chapter_id.in_(chapter_ids))
    return list((await session.execute(stmt)).scalars().all())


async def _search_pass(
    session: AsyncSession,
    query: str,
    query_embedding: list[float],
    scope: str,
    course_id: int,
    owner_id: int | None,
    chapter_ids: list[int] | None,
    entry: _Bm25Entry | None,
    permission_rows: list[Chunk] | None,
    candidate_k: int,
    index_info: dict,
    rrf_k: int,
    semantic_weight: float,
    bm25_weight: float,
) -> tuple[list[dict], dict]:
    degraded: list[str] = []
    semantic_hits: list[Chunk] = []
    bm25_hits: list[Chunk] = []
    if index_info["semantic_enabled"]:
        try:
            semantic_hits = await semantic_search(
                session, query_embedding, scope, course_id, owner_id, chapter_ids, top_k=candidate_k
            )
        except Exception:
            degraded.append("semantic")
            logger.warning("语义召回失败，降级 BM25 course=%s scope=%s", course_id, scope, exc_info=True)
    observed_versions = {
        version for chunk in semantic_hits
        if (version := getattr(chunk, "_rag_index_version", None)) is not None
    }
    observed_models = {
        model for chunk in semantic_hits
        if (model := getattr(chunk, "_rag_embedding_model", None)) is not None
    }
    expected_version = index_info.get("index_version")
    expected_model = index_info.get("embedding_model")
    if semantic_hits and (
        (expected_version and observed_versions != {expected_version})
        or (expected_model and observed_models != {expected_model})
    ):
        semantic_hits = []
        degraded.append("index_version_mismatch")

    if entry is not None:
        allowed = set(entry.chunk_ids)
        if chapter_ids:
            chapter_set = set(chapter_ids)
            allowed = {
                cid
                for cid, chapter_id in zip(entry.chunk_ids, entry.chapter_ids, strict=True)
                if chapter_id in chapter_set
            }
    else:
        rows = permission_rows or []
        allowed = {chunk.chunk_id for chunk in rows}
    semantic_hits = [chunk for chunk in semantic_hits if chunk.chunk_id in allowed]

    try:
        if entry is None:
            raise RuntimeError("BM25 语料不可用")
        bm25_ids = await asyncio.to_thread(
            _bm25_top_ids, entry, query, chapter_ids, candidate_k
        )
        if bm25_ids:
            rows = (
                await session.execute(select(Chunk).where(Chunk.chunk_id.in_(bm25_ids)))
            ).scalars().all()
            by_id = {chunk.chunk_id: chunk for chunk in rows}
            bm25_hits = [by_id[cid] for cid in bm25_ids if cid in by_id and cid in allowed]
    except Exception:
        degraded.append("bm25")
        logger.warning("BM25 召回失败，降级语义路 course=%s scope=%s", course_id, scope, exc_info=True)

    fused = rrf_fuse(
        semantic_hits,
        bm25_hits,
        top_k=candidate_k,
        k=rrf_k,
        weight_a=semantic_weight,
        weight_b=bm25_weight,
    )
    by_id = {chunk.chunk_id: chunk for chunk in [*semantic_hits, *bm25_hits]}
    semantic_ids = {chunk.chunk_id for chunk in semantic_hits}
    bm25_ids = {chunk.chunk_id for chunk in bm25_hits}
    hits = [
        {
            "chunk": by_id[chunk_id],
            "score": score,
            "sources": [
                name
                for name, present in (
                    ("semantic", chunk_id in semantic_ids),
                    ("bm25", chunk_id in bm25_ids),
                )
                if present
            ],
        }
        for chunk_id, score in fused
        if chunk_id in by_id
    ]
    metrics = {
        "semantic_candidates": len(semantic_hits),
        "bm25_candidates": len(bm25_hits),
        "union_candidates": len({chunk.chunk_id for chunk in [*semantic_hits, *bm25_hits]}),
        "degraded": degraded,
        "observed_index_versions": sorted(observed_versions),
        "observed_embedding_models": sorted(observed_models),
        "distances": {
            chunk.chunk_id: getattr(chunk, "_rag_distance", None) for chunk in semantic_hits
        },
    }
    return hits, metrics


async def fine_search(
    session: AsyncSession,
    query: str,
    query_embedding: list[float],
    scope: str,
    course_id: int,
    owner_id: int | None,
    chapter_ids: list[int] | None = None,
    top_k: int | None = None,
    candidate_k: int | None = None,
    min_hits: int = 3,
) -> SearchResults:
    """双路 RRF、全局补充和证据硬门槛；索引重建中仅使用 PG/BM25。"""
    settings = get_settings()
    top_k = top_k or settings.rag_top_k
    candidate_k = candidate_k or settings.rag_candidate_k
    index_info = await _index_info(scope)
    metrics: dict = {
        "fallback": [],
        "degraded": [],
        **{key: value for key, value in index_info.items() if key != "semantic_enabled"},
        "target_index_version": INDEX_VERSION,
    }
    if not index_info["semantic_enabled"]:
        metrics["degraded"].append("semantic_not_ready")
        metrics["fallback"].append(f"index_{index_info['index_state']}")
    if scope == "personal" and owner_id is None:
        metrics["degraded"].append("missing_owner")
        return SearchResults(metrics=metrics)
    permission_rows: list[Chunk] | None = None
    try:
        entry = await _get_bm25_entry(session, course_id, scope, owner_id)
    except Exception:
        entry = None
        permission_rows = await _permission_chunks(
            session, course_id, scope, owner_id, chapter_ids=None
        )
        if not permission_rows:
            metrics["degraded"].extend(["bm25", "empty_corpus"])
            return SearchResults(metrics=metrics)
    else:
        if entry is None:
            metrics["fallback"].append("empty_corpus")
            return SearchResults(metrics=metrics)

    async def search_pass(chapter_filter):
        return await _search_pass(
            session, query, query_embedding, scope, course_id, owner_id,
            chapter_filter, entry, permission_rows, candidate_k, index_info,
            settings.rag_rrf_k, settings.rag_semantic_weight, settings.rag_bm25_weight,
        )

    hits, first_metrics = await search_pass(chapter_ids)
    metrics.update({key: value for key, value in first_metrics.items() if key != "degraded"})
    metrics["degraded"].extend(first_metrics["degraded"])
    raw_count = len(hits)

    def filter_hits(items: list[dict]) -> tuple[list[dict], dict]:
        return filter_sufficient_evidence(
            query,
            items,
            max_distance=settings.rag_evidence_max_distance,
            min_lexical_coverage=settings.rag_evidence_min_lexical_coverage,
            semantic_weak_lexical_min_similarity=(
                settings.rag_evidence_semantic_weak_lexical_min_similarity
            ),
            degraded=metrics["degraded"],
        )

    accepted, evidence_metrics = filter_hits(hits)
    # 全局补充按“通过证据门槛的候选”判断，而不是原始融合候选：
    # 章节内 TopK 可能全是弱证据，原始候选数会掩盖漏召回（R3）。
    fallback_reasons = needs_global_fallback(
        accepted,
        chapter_ids=chapter_ids,
        min_hits=min_hits,
        max_distance=settings.rag_global_fallback_max_distance,
    )
    coarse_incomplete = bool(getattr(chapter_ids, "incomplete", False))
    has_unmapped = bool(entry and any(chapter_id is None for chapter_id in entry.chapter_ids))
    if chapter_ids and not fallback_reasons and (has_unmapped or coarse_incomplete):
        try:
            global_bm25_ids = await asyncio.to_thread(
                _bm25_top_ids, entry, query, None, candidate_k
            )
        except Exception:
            global_bm25_ids = []
            metrics["degraded"].append("bm25")
        chapter_by_id = dict(zip(entry.chunk_ids, entry.chapter_ids, strict=True))
        outside = {chapter_by_id.get(chunk_id) for chunk_id in global_bm25_ids}
        if None in outside:
            fallback_reasons.append("unmapped_chunks")
        if coarse_incomplete and outside.difference({None, *chapter_ids}):
            fallback_reasons.append("no_summary_chapters")
    if fallback_reasons:
        global_hits, global_metrics = await search_pass(None)
        metrics["fallback"].extend(fallback_reasons)
        metrics["degraded"].extend(global_metrics["degraded"])
        metrics["global_candidates"] = global_metrics["union_candidates"]
        fused = rrf_fuse(
            [hit["chunk"] for hit in accepted],
            [hit["chunk"] for hit in global_hits],
            # 全局补充是为纠正弱章节证据，权重高于原章节候选；否则同样秩次下
            # 弱章节命中会与全课程强命中并列，错章仍排在前面（v1.3 实测）。
            weight_a=1.0,
            weight_b=2.0,
            top_k=candidate_k,
        )
        combined = [*accepted, *global_hits]
        by_id = {hit["chunk"].chunk_id: hit["chunk"] for hit in combined}
        hits = [
            {
                "chunk": by_id[chunk_id], "score": score, "fallback": True,
                "sources": next(
                    (hit.get("sources", []) for hit in combined if hit["chunk"].chunk_id == chunk_id),
                    [],
                ),
            }
            for chunk_id, score in fused
            if chunk_id in by_id
        ]
        accepted, evidence_metrics = filter_hits(hits)
    metrics["degraded"] = list(dict.fromkeys(metrics["degraded"]))
    metrics["raw_candidates"] = raw_count
    metrics.update(evidence_metrics)
    hits = accepted[:top_k]
    metrics["returned_candidates"] = len(hits)
    return SearchResults(hits, metrics=metrics)
