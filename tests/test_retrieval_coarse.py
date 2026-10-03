"""检索链路单元测试：章节粗召回（coarse）与知识点精排（fine）主路径。

与 test_perf.py / test_rag.py 同一约定：CI 无 DB/Chroma，session 与向量库
均为内存替身（FakeSession / FakeVectorStore），BM25 缓存每个用例前后清空。
test_perf.py 已覆盖缓存命中/失效/章节过滤，本文件补齐语义召回、双路融合、
TTL 过期与 LRU 逐出等主路径。
"""

from types import SimpleNamespace

import pytest

import app.core.retrieval.coarse as coarse
import app.core.retrieval.fine as fine
from app.storage.vector_store import COLLECTION_PERSONAL, COLLECTION_PUBLIC


@pytest.fixture(autouse=True)
def _clear_bm25_cache():
    """每个用例前后清空 BM25 进程内缓存，避免相互污染。"""
    fine._bm25_cache.clear()
    yield
    fine._bm25_cache.clear()


class _AllResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ScalarsResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class FakeVectorStore:
    """内存向量库替身：记录集合名与查询参数，返回预置 hits。"""

    def __init__(self, hits=()):
        self._hits = list(hits)
        self.collections: list[str] = []
        self.queries: list[dict] = []

    async def get_or_create_collection(self, name):
        self.collections.append(name)
        return name

    async def query(self, collection, embedding, n_results=10, where=None):
        self.queries.append({"collection": collection, "n_results": n_results, "where": where})
        return list(self._hits)


# ---------------------------------------------------------------------------
# 章节粗召回（app/core/retrieval/coarse.py）
# ---------------------------------------------------------------------------


class CoarseSession:
    """粗召回假会话：execute 固定返回预置 Chapter 列表。"""

    def __init__(self, chapters):
        self._chapters = chapters
        self.calls = 0

    async def execute(self, stmt):
        self.calls += 1
        return _ScalarsResult(self._chapters)


def _chapter(chapter_id, vector_id):
    return SimpleNamespace(id=chapter_id, course_id=7, summary_vector_id=vector_id)


async def test_coarse_recall_no_summary_vectors_returns_none(monkeypatch):
    """无章节摘要向量：直接降级 None，不触达向量库（§8.3 降级路径）。"""

    def _boom():
        raise AssertionError("不应触达向量库")

    monkeypatch.setattr(coarse, "get_vector_store", _boom)
    session = CoarseSession([])
    assert await coarse.coarse_recall(session, [0.1], 7, "public") is None


async def test_coarse_recall_maps_vector_ids_to_chapters(monkeypatch):
    """摘要向量命中按 id 映射回章节，未匹配到的向量 id 丢弃，保持命中顺序。"""
    store = FakeVectorStore([{"id": "sv2"}, {"id": "sv_unknown"}, {"id": "sv1"}])
    monkeypatch.setattr(coarse, "get_vector_store", lambda: store)
    session = CoarseSession([_chapter(1, "sv1"), _chapter(2, "sv2")])
    chapter_ids = await coarse.coarse_recall(session, [0.1], 7, "public", top_k=3)
    assert chapter_ids == [2, 1]
    assert store.collections == [coarse.COLLECTION_SUMMARIES]
    assert store.queries[0]["where"] == {"course_id": 7}  # 课程过滤双层执行
    assert store.queries[0]["n_results"] == 3


async def test_coarse_recall_all_hits_unmatched_returns_none(monkeypatch):
    """向量库有命中但都不属于本课程章节（脏数据）：返回 None 走降级。"""
    store = FakeVectorStore([{"id": "sv_unknown"}])
    monkeypatch.setattr(coarse, "get_vector_store", lambda: store)
    session = CoarseSession([_chapter(1, "sv1")])
    assert await coarse.coarse_recall(session, [0.1], 7, "public") is None


async def test_coarse_recall_empty_hits_returns_none(monkeypatch):
    store = FakeVectorStore([])
    monkeypatch.setattr(coarse, "get_vector_store", lambda: store)
    session = CoarseSession([_chapter(1, "sv1")])
    assert await coarse.coarse_recall(session, [0.1], 7, "personal") is None


# ---------------------------------------------------------------------------
# Chroma where 过滤（fine._build_where，纯函数）
# ---------------------------------------------------------------------------


def test_build_where_course_only():
    assert fine._build_where("public", 7, None, None) == {"course_id": 7}


def test_build_where_personal_owner_and_chapters():
    where = fine._build_where("personal", 7, 42, [1, 2])
    assert where == {
        "$and": [
            {"course_id": 7},
            {"owner_id": 42},
            {"chapter_id": {"$in": [1, 2]}},
        ]
    }


def test_build_where_personal_without_owner():
    """owner_id 为 None 时不加 owner 条件（personal 双层过滤的兜底）。"""
    assert fine._build_where("personal", 7, None, None) == {"course_id": 7}


def test_build_where_public_with_chapters():
    where = fine._build_where("public", 7, 42, [3])
    assert where == {"$and": [{"course_id": 7}, {"chapter_id": {"$in": [3]}}]}


# ---------------------------------------------------------------------------
# 语义召回（fine.semantic_search）
# ---------------------------------------------------------------------------


class FineSession:
    """精排假会话：3 列查询视为 BM25 语料加载，其余视为按 id 取 Chunk 行。"""

    def __init__(self, corpus_rows=(), chunks=None):
        self._corpus_rows = list(corpus_rows)  # [(chunk_id, chapter_id, content)]
        self._chunks = chunks or {}  # {chunk_id: namespace}
        self.corpus_calls = 0
        self.chunk_calls = 0

    async def execute(self, stmt):
        if len(stmt.column_descriptions) == 3:
            self.corpus_calls += 1
            return _AllResult(list(self._corpus_rows))
        self.chunk_calls += 1
        return _ScalarsResult(list(self._chunks.values()))


def _corpus():
    return [
        ("c1", 1, "分页存储管理把逻辑地址空间划分为固定大小的页"),
        ("c2", 2, "编译原理中的词法分析器负责识别单词符号"),
        ("c3", 1, "分页与分段的区别在于页是定长的物理划分"),
    ]


def _chunks(*ids):
    return {cid: SimpleNamespace(chunk_id=cid) for cid in ids}


async def test_semantic_search_public_collection_and_order(monkeypatch):
    """public 走 chunks_public 集合；结果按向量命中顺序，PG 中不存在的 id 丢弃。"""
    store = FakeVectorStore([{"id": "c2"}, {"id": "c1"}, {"id": "ghost"}])
    monkeypatch.setattr(fine, "get_vector_store", lambda: store)
    session = FineSession(chunks=_chunks("c1", "c2"))
    hits = await fine.semantic_search(session, [0.1], "public", 7, None)
    assert [c.chunk_id for c in hits] == ["c2", "c1"]
    assert store.collections == [COLLECTION_PUBLIC]
    assert store.queries[0]["where"] == {"course_id": 7}


async def test_semantic_search_personal_collection_and_where(monkeypatch):
    """personal 走 chunks_personal 集合，where 带 course + owner + chapter 过滤。"""
    store = FakeVectorStore([{"id": "c1"}])
    monkeypatch.setattr(fine, "get_vector_store", lambda: store)
    session = FineSession(chunks=_chunks("c1"))
    hits = await fine.semantic_search(session, [0.1], "personal", 7, 42, chapter_ids=[1])
    assert [c.chunk_id for c in hits] == ["c1"]
    assert store.collections == [COLLECTION_PERSONAL]
    assert store.queries[0]["where"] == {
        "$and": [
            {"course_id": 7},
            {"owner_id": 42},
            {"chapter_id": {"$in": [1]}},
        ]
    }


async def test_semantic_search_empty_hits_skips_pg(monkeypatch):
    """向量库无命中：直接返回空，不回表 PG。"""
    store = FakeVectorStore([])
    monkeypatch.setattr(fine, "get_vector_store", lambda: store)
    session = FineSession(chunks=_chunks("c1"))
    assert await fine.semantic_search(session, [0.1], "public", 7, None) == []
    assert session.chunk_calls == 0


# ---------------------------------------------------------------------------
# 双路融合精排（fine.fine_search）
# ---------------------------------------------------------------------------


async def test_fine_search_empty_corpus_returns_empty(monkeypatch):
    """BM25 语料为空：直接返回空，不做语义召回。"""
    called = False

    async def _semantic(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(fine, "semantic_search", _semantic)
    session = FineSession(corpus_rows=[], chunks=_chunks("c1"))
    assert await fine.fine_search(session, "分页存储", [0.0], "public", 7, None) == []
    assert not called
    assert session.corpus_calls == 1


async def test_fine_search_fuses_semantic_and_bm25(monkeypatch):
    """双路 RRF 融合：两路都命中的 c1 排最前；不在缓存语料内的语义命中被过滤。"""
    store = FakeVectorStore([{"id": "c2"}, {"id": "c1"}, {"id": "c_outside"}])
    monkeypatch.setattr(fine, "get_vector_store", lambda: store)
    chunks = _chunks("c1", "c2", "c3", "c_outside")
    session = FineSession(corpus_rows=_corpus(), chunks=chunks)
    hits = await fine.fine_search(session, "分页存储", [0.0], "public", 7, None)
    ids = [h["chunk"].chunk_id for h in hits]
    assert ids == ["c1", "c2", "c3"]  # c_outside 不在课程语料内，被权限边界过滤
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert store.queries[0]["n_results"] == 10  # 语义路固定 top10


async def test_fine_search_chapter_filter_applied_both_ways(monkeypatch):
    """chapter_ids 在语义侧（allowed 集合）与 BM25 侧（缓存语料打分）各自过滤。"""
    store = FakeVectorStore([{"id": "c2"}, {"id": "c1"}])
    monkeypatch.setattr(fine, "get_vector_store", lambda: store)
    session = FineSession(corpus_rows=_corpus(), chunks=_chunks("c1", "c2", "c3"))
    hits = await fine.fine_search(session, "分页存储", [0.0], "public", 7, None, chapter_ids=[1])
    ids = [h["chunk"].chunk_id for h in hits]
    assert ids == ["c1", "c3"]  # c2 属第 2 章，被过滤
    assert store.queries[0]["where"] == {
        "$and": [{"course_id": 7}, {"chapter_id": {"$in": [1]}}]
    }


async def test_fine_search_drops_chunks_missing_from_pg(monkeypatch):
    """BM25 命中的 chunk 在 PG 回表时缺失（脏数据）：丢弃且不进入融合结果。"""
    store = FakeVectorStore([])  # 语义路无命中
    monkeypatch.setattr(fine, "get_vector_store", lambda: store)
    session = FineSession(corpus_rows=_corpus(), chunks=_chunks("c1"))  # c3 回表缺失
    hits = await fine.fine_search(session, "分页存储", [0.0], "public", 7, None)
    assert [h["chunk"].chunk_id for h in hits] == ["c1"]


# ---------------------------------------------------------------------------
# BM25 缓存：TTL 过期与 LRU 逐出（test_perf.py 之外的边界）
# ---------------------------------------------------------------------------


async def test_bm25_entry_rebuilt_after_ttl_expired(monkeypatch):
    """TTL 过期后回源重建（精准失效之外的兜底路径）。"""
    monkeypatch.setattr(fine, "_BM25_CACHE_TTL", -1.0)  # 立即过期
    session = FineSession(corpus_rows=_corpus(), chunks=_chunks("c1"))
    first = await fine._get_bm25_entry(session, 7, "public", None)
    second = await fine._get_bm25_entry(session, 7, "public", None)
    assert first is not second
    assert session.corpus_calls == 2


async def test_bm25_cache_lru_eviction(monkeypatch):
    """缓存超过上限按 LRU 逐出最久未用项。"""
    monkeypatch.setattr(fine, "_BM25_CACHE_MAX", 1)
    session = FineSession(corpus_rows=_corpus(), chunks=_chunks("c1"))
    first = await fine._get_bm25_entry(session, 7, "public", None)
    await fine._get_bm25_entry(session, 8, "public", None)  # 挤掉 course 7
    assert len(fine._bm25_cache) == 1
    third = await fine._get_bm25_entry(session, 7, "public", None)
    assert third is not first
    assert session.corpus_calls == 3
