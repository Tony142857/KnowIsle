"""v0.9 性能优化单元测试：BM25 语料缓存、platform_config TTL 缓存、
Chroma 集合 ID 缓存与 query payload、ILIKE 转义纯函数。

与 test_rag.py / test_platform_config.py 同一约定：CI 无 DB/Redis/Chroma，
全部为内存构造测试（Fake Session / monkeypatch 替身）。
"""

from types import SimpleNamespace

import pytest
from rank_bm25 import BM25Okapi as _RealBM25

import app.core.platform_config as platform_config
import app.core.retrieval.fine as fine
import app.storage.vector_store as vector_store
from app.api.resources import escape_like


@pytest.fixture(autouse=True)
def _clear_caches():
    """每个用例前后清空进程内缓存，避免用例间相互污染。"""
    fine._bm25_cache.clear()
    platform_config._config_cache.clear()
    vector_store._collection_ids.clear()
    yield
    fine._bm25_cache.clear()
    platform_config._config_cache.clear()
    vector_store._collection_ids.clear()


# ---------------------------------------------------------------------------
# BM25 语料缓存（app/core/retrieval/fine.py）
# ---------------------------------------------------------------------------


class _CountingBM25:
    """BM25Okapi 替身：统计构建次数，行为委托给真实实现。"""

    builds = 0

    def __init__(self, corpus):
        type(self).builds += 1
        self._inner = _RealBM25(corpus)

    def get_scores(self, query_tokens):
        return self._inner.get_scores(query_tokens)


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


class _ChunkSession:
    """细粒度假会话：3 列查询视为 BM25 语料加载（计数），其余视为按 id 取 Chunk 行。"""

    def __init__(self, corpus_rows, chunks):
        self._corpus_rows = corpus_rows  # [(chunk_id, chapter_id, content)]
        self._chunks = chunks  # {chunk_id: namespace}
        self.corpus_calls = 0

    async def execute(self, stmt):
        if len(stmt.column_descriptions) == 3:
            self.corpus_calls += 1
            return _AllResult(self._corpus_rows)
        return _ScalarsResult(list(self._chunks.values()))


def _corpus(course_chapter=None):
    return [
        ("c1", 1, "分页存储管理把逻辑地址空间划分为固定大小的页"),
        ("c2", 2, "编译原理中的词法分析器负责识别单词符号"),
        ("c3", 1, "分页与分段的区别在于页是定长的物理划分"),
    ]


def _chunks_ns():
    return {cid: SimpleNamespace(chunk_id=cid) for cid in ("c1", "c2", "c3")}


async def test_bm25_entry_cached_on_second_call(monkeypatch):
    """同参数二次调用命中缓存：不再查库、不重建 BM25。"""
    monkeypatch.setattr(fine, "BM25Okapi", _CountingBM25)
    _CountingBM25.builds = 0
    session = _ChunkSession(_corpus(), _chunks_ns())
    first = await fine._get_bm25_entry(session, 7, "public", None)
    second = await fine._get_bm25_entry(session, 7, "public", None)
    assert first is second
    assert session.corpus_calls == 1
    assert _CountingBM25.builds == 1


async def test_bm25_entry_rebuilt_after_invalidate(monkeypatch):
    """invalidate_bm25 后重建索引。"""
    monkeypatch.setattr(fine, "BM25Okapi", _CountingBM25)
    _CountingBM25.builds = 0
    session = _ChunkSession(_corpus(), _chunks_ns())
    await fine._get_bm25_entry(session, 7, "public", None)
    fine.invalidate_bm25(7, "public")
    entry = await fine._get_bm25_entry(session, 7, "public", None)
    assert entry is not None
    assert session.corpus_calls == 2
    assert _CountingBM25.builds == 2


async def test_bm25_cache_keys_isolated():
    """不同 (course_id, scope, owner_id) 互不污染；失效一个不影响另一个。"""
    session = _ChunkSession(_corpus(), _chunks_ns())
    pub = await fine._get_bm25_entry(session, 7, "public", None)
    per = await fine._get_bm25_entry(session, 7, "personal", 42)
    other_course = await fine._get_bm25_entry(session, 8, "public", None)
    assert len({id(pub), id(per), id(other_course)}) == 3
    fine.invalidate_bm25(7, "personal", 42)
    assert await fine._get_bm25_entry(session, 7, "public", None) is pub
    assert await fine._get_bm25_entry(session, 8, "public", None) is other_course
    assert await fine._get_bm25_entry(session, 7, "personal", 42) is not per


async def test_bm25_public_owner_normalized():
    """public 语料不按 owner 过滤：owner_id 任意取值归一为同一缓存键。"""
    session = _ChunkSession(_corpus(), _chunks_ns())
    a = await fine._get_bm25_entry(session, 7, "public", None)
    b = await fine._get_bm25_entry(session, 7, "public", 999)
    assert a is b
    assert session.corpus_calls == 1


def test_bm25_top_ids_chapter_filter():
    """chapter_ids 过滤：只取属于指定章节的 chunk，且保持得分降序、分数>0。"""
    entry = fine._build_bm25_entry(_corpus())
    ids_all = fine._bm25_top_ids(entry, "分页存储", None, 10)
    assert set(ids_all) <= {"c1", "c3"}
    assert ids_all  # 相关命中非空
    ids_ch1 = fine._bm25_top_ids(entry, "分页存储", [1], 10)
    assert set(ids_ch1) == set(ids_all)  # c1/c3 均属第 1 章
    ids_ch2 = fine._bm25_top_ids(entry, "分页存储", [2], 10)
    assert ids_ch2 == []  # 第 2 章只有不相关的 c2


async def test_fine_search_reuses_bm25_cache(monkeypatch):
    """fine_search 全链路：同 key 两次检索只建一次 BM25、只查一次语料。"""
    monkeypatch.setattr(fine, "BM25Okapi", _CountingBM25)
    _CountingBM25.builds = 0

    async def _no_semantic(*args, **kwargs):
        return []

    monkeypatch.setattr(fine, "semantic_search", _no_semantic)
    session = _ChunkSession(_corpus(), _chunks_ns())
    hits1 = await fine.fine_search(session, "分页存储", [0.0], "public", 7, None)
    hits2 = await fine.fine_search(session, "分页存储", [0.0], "public", 7, None)
    assert hits1 and hits2
    assert {h["chunk"].chunk_id for h in hits1} <= {"c1", "c3"}
    assert session.corpus_calls == 1
    assert _CountingBM25.builds == 1


# ---------------------------------------------------------------------------
# platform_config 进程内 TTL 缓存（app/core/platform_config.py）
# ---------------------------------------------------------------------------


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ConfigSession:
    """细粒度假会话：execute 返回当前 db_value，add 仅记录。"""

    def __init__(self, db_value):
        self.db_value = db_value  # 标量（get_config）或行（set_config），按测试场景注入
        self.calls = 0
        self.added = []

    async def execute(self, stmt):
        self.calls += 1
        return _ScalarResult(self.db_value)

    def add(self, obj):
        self.added.append(obj)


async def test_get_config_cached_within_ttl():
    session = _ConfigSession(50)
    assert await platform_config.get_config(session, "ai_daily_limit") == 50
    assert await platform_config.get_config(session, "ai_daily_limit") == 50
    assert session.calls == 1  # 第二次命中缓存未查库


async def test_get_config_refetches_after_ttl_expired(monkeypatch):
    monkeypatch.setattr(platform_config, "_CONFIG_CACHE_TTL", -1.0)  # 立即过期
    session = _ConfigSession(50)
    assert await platform_config.get_config(session, "ai_daily_limit") == 50
    session.db_value = 60
    assert await platform_config.get_config(session, "ai_daily_limit") == 60
    assert session.calls == 2  # TTL 过期后回源


async def test_set_config_invalidates_cache():
    """set 后立即读到新值：缓存失效，下一次 get 回源拿到写入值。"""
    get_session = _ConfigSession(50)
    assert await platform_config.get_config(get_session, "ai_daily_limit") == 50
    row = SimpleNamespace(value=50, updated_by=None)
    set_session = _ConfigSession(row)
    old, new = await platform_config.set_config(set_session, "ai_daily_limit", 80, admin_id=1)
    assert (old, new) == (50, 80)
    assert row.value == 80
    get_session.db_value = 80  # 模拟写入已生效
    assert await platform_config.get_config(get_session, "ai_daily_limit") == 80
    assert get_session.calls == 2  # 失效生效：没有读到缓存里的旧值 50


async def test_get_config_falls_back_to_default():
    session = _ConfigSession(None)
    default = platform_config.config_specs()["ai_daily_limit"].default
    assert await platform_config.get_config(session, "ai_daily_limit") == default


# ---------------------------------------------------------------------------
# Chroma 集合 ID 缓存与 query payload（app/storage/vector_store.py）
# ---------------------------------------------------------------------------


class _FakeChromaResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeChromaClient:
    is_closed = False

    def __init__(self, payload):
        self._payload = payload
        self.posts = 0
        self.last_json = None

    async def post(self, url, json=None, timeout=None):
        self.posts += 1
        self.last_json = json
        return _FakeChromaResponse(self._payload)


async def test_collection_id_cached(monkeypatch):
    """collection name→id 进程内缓存：第二次 get_or_create 不发请求。"""
    client = _FakeChromaClient({"id": "cid-1"})
    monkeypatch.setitem(vector_store._clients, "http://fake-chroma", client)
    store = vector_store.VectorStore("http://fake-chroma")
    assert await store.get_or_create_collection("chunks_public") == "cid-1"
    assert await store.get_or_create_collection("chunks_public") == "cid-1"
    assert client.posts == 1


async def test_query_payload_excludes_documents(monkeypatch):
    """query 的 include 不含 documents（消费方只用 id 回表 PG）。"""
    payload = {"ids": [["c1"]], "distances": [[0.1]], "metadatas": [[{"k": "v"}]]}
    client = _FakeChromaClient(payload)
    monkeypatch.setitem(vector_store._clients, "http://fake-chroma", client)
    store = vector_store.VectorStore("http://fake-chroma")
    hits = await store.query("cid-1", [0.0], n_results=5)
    assert client.last_json["include"] == ["metadatas", "distances"]
    assert hits == [{"id": "c1", "distance": 0.1, "metadata": {"k": "v"}}]


# ---------------------------------------------------------------------------
# ILIKE 转义纯函数（app/api/resources.py）
# ---------------------------------------------------------------------------


def test_escape_like_special_chars():
    assert escape_like("100%") == "100\\%"
    assert escape_like("a_b") == "a\\_b"
    assert escape_like("50%_off") == "50\\%\\_off"


def test_escape_like_backslash_first():
    # 反斜杠必须先转义，否则会对后加的转义符二次转义
    assert escape_like("a\\b") == "a\\\\b"
    assert escape_like("\\%") == "\\\\\\%"


def test_escape_like_plain_text_unchanged():
    assert escape_like("操作系统 2023") == "操作系统 2023"
    assert escape_like("") == ""
