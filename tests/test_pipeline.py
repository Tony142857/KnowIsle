"""RAG 主流水线单元测试（app/core/pipeline.py）：课程权限检查、双库检索编排、
SSE 事件流（token → citations → done / error）、引用映射与 qa_logs 落库。

与 test_growth.py / test_perf.py 同一约定：CI 无 DB/LLM/embedding，session、
LLM client、embedding 均为内存替身；retrieve 编排内 coarse/fine 以
monkeypatch 假实现隔离，检索细节见 tests/test_retrieval_coarse.py。
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.core.pipeline as pipeline
from app.core.llm.router import ModelTier
from app.storage.models import QaLog


class _AllResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeSession:
    """流水线假会话：get 返回预置课程，execute 返回引用查询行，add/commit 记录。"""

    def __init__(self, course=None, citation_rows=(), fail_commit=False):
        self._course = course
        self._citation_rows = list(citation_rows)
        self._fail_commit = fail_commit
        self.added: list = []
        self.executes = 0
        self.commits = 0
        self.rollbacks = 0

    async def get(self, model, pk):
        return self._course

    async def execute(self, stmt):
        self.executes += 1
        return _AllResult(list(self._citation_rows))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        if self._fail_commit:
            raise RuntimeError("db down")
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class FakeEmbedding:
    async def embed(self, texts):
        return [[0.1, 0.2] for _ in texts]


class FakeLLMClient:
    """流式 LLM 替身：按预置 token 逐个产出，可模拟异常与 usage。"""

    def __init__(self, tokens=(), usage=None, fail=False):
        self._tokens = list(tokens)
        self._usage = usage
        self._fail = fail
        self.last_usage = None
        self.messages = None

    async def chat_stream(self, messages):
        self.messages = messages
        if self._fail:
            raise RuntimeError("llm down")
        for token in self._tokens:
            yield token
        self.last_usage = self._usage


def _user(user_id=7):
    return SimpleNamespace(id=user_id)


def _course(scope="personal", owner_id=7, status="active"):
    return SimpleNamespace(id=1, scope=scope, owner_id=owner_id, status=status)


def _body(scope="personal"):
    return SimpleNamespace(question="什么是分页存储", course_id=7, scope=scope)


def _chunk(chunk_id="per_d1_00001", content="分页存储管理把逻辑地址空间划分为固定大小的页。"):
    return SimpleNamespace(
        chunk_id=chunk_id, content=content, section="3.1 分页存储",
        page_num=12, scope="personal",
    )


# ---------------------------------------------------------------------------
# check_course_access：课程存在性与数据级权限（越权 404）
# ---------------------------------------------------------------------------


async def test_check_course_access_course_missing():
    session = FakeSession(course=None)
    with pytest.raises(HTTPException) as exc:
        await pipeline.check_course_access(session, _user(), 1, "personal")
    assert exc.value.status_code == 404


async def test_check_course_access_personal_owner_ok():
    course = _course(scope="personal", owner_id=7)
    got = await pipeline.check_course_access(FakeSession(course), _user(7), 1, "personal")
    assert got is course


async def test_check_course_access_personal_wrong_owner():
    session = FakeSession(_course(scope="personal", owner_id=8))
    with pytest.raises(HTTPException):
        await pipeline.check_course_access(session, _user(7), 1, "personal")


async def test_check_course_access_personal_rejects_public_course():
    session = FakeSession(_course(scope="public", owner_id=None))
    with pytest.raises(HTTPException):
        await pipeline.check_course_access(session, _user(7), 1, "personal")


async def test_check_course_access_public_active_ok():
    course = _course(scope="public", owner_id=None, status="active")
    got = await pipeline.check_course_access(FakeSession(course), _user(7), 1, "public")
    assert got is course


async def test_check_course_access_public_pending_rejected():
    session = FakeSession(_course(scope="public", owner_id=None, status="pending"))
    with pytest.raises(HTTPException):
        await pipeline.check_course_access(session, _user(7), 1, "public")


async def test_check_course_access_public_rejects_personal_course():
    session = FakeSession(_course(scope="personal", owner_id=7))
    with pytest.raises(HTTPException):
        await pipeline.check_course_access(session, _user(7), 1, "public")


async def test_check_course_access_mixed_own_personal_ok():
    course = _course(scope="personal", owner_id=7)
    got = await pipeline.check_course_access(FakeSession(course), _user(7), 1, "mixed")
    assert got is course


async def test_check_course_access_mixed_others_personal_rejected():
    session = FakeSession(_course(scope="personal", owner_id=8))
    with pytest.raises(HTTPException):
        await pipeline.check_course_access(session, _user(7), 1, "mixed")


async def test_check_course_access_mixed_public_active_ok():
    course = _course(scope="public", owner_id=None, status="active")
    got = await pipeline.check_course_access(FakeSession(course), _user(7), 1, "mixed")
    assert got is course


async def test_check_course_access_mixed_public_pending_rejected():
    session = FakeSession(_course(scope="public", owner_id=None, status="disabled"))
    with pytest.raises(HTTPException):
        await pipeline.check_course_access(session, _user(7), 1, "mixed")


# ---------------------------------------------------------------------------
# retrieve：scope 路由编排（personal/public 粗召回→精排；mixed 双库 RRF）
# ---------------------------------------------------------------------------


async def test_retrieve_personal_passes_chapter_ids(monkeypatch):
    """personal：粗召回章节范围传给精排，返回 (hits, chapter_ids)。"""
    monkeypatch.setattr(pipeline, "get_embedding", lambda: FakeEmbedding())
    seen = {}

    async def fake_coarse(session, query_embedding, course_id, scope):
        return [3, 4]

    async def fake_fine(session, query, query_embedding, scope, course_id,
                        owner_id=None, chapter_ids=None):
        seen.update(scope=scope, owner_id=owner_id, chapter_ids=chapter_ids)
        return [{"chunk": _chunk(), "score": 0.1}]

    monkeypatch.setattr(pipeline, "coarse_recall", fake_coarse)
    monkeypatch.setattr(pipeline, "fine_search", fake_fine)
    session = FakeSession(_course(scope="personal", owner_id=7))
    hits, chapter_ids = await pipeline.retrieve(session, "q", _user(7), 7, "personal")
    assert chapter_ids == [3, 4]
    assert seen == {"scope": "personal", "owner_id": 7, "chapter_ids": [3, 4]}
    assert hits[0]["chunk"].chunk_id == "per_d1_00001"


async def test_retrieve_public_degrades_when_coarse_none(monkeypatch):
    """粗召回返回 None（无章节摘要）：chapter_ids=None 降级全课程精排（§8.3）。"""
    monkeypatch.setattr(pipeline, "get_embedding", lambda: FakeEmbedding())
    seen = {}

    async def fake_coarse(session, query_embedding, course_id, scope):
        return None

    async def fake_fine(session, query, query_embedding, scope, course_id,
                        owner_id=None, chapter_ids=None):
        seen.update(scope=scope, chapter_ids=chapter_ids)
        return []

    monkeypatch.setattr(pipeline, "coarse_recall", fake_coarse)
    monkeypatch.setattr(pipeline, "fine_search", fake_fine)
    session = FakeSession(_course(scope="public", owner_id=None))
    hits, chapter_ids = await pipeline.retrieve(session, "q", _user(7), 7, "public")
    assert hits == [] and chapter_ids is None
    assert seen == {"scope": "public", "chapter_ids": None}


async def test_retrieve_mixed_fuses_with_personal_boost(monkeypatch):
    """mixed：双库串行检索，个人库加权 1.2 后 RRF 融合，个人块压过公共块。"""
    monkeypatch.setattr(pipeline, "get_embedding", lambda: FakeEmbedding())
    calls = []
    per_chunk = _chunk("per_d1_00001")
    pub_chunk = _chunk("pub_d1_00001")

    async def fake_fine(session, query, query_embedding, scope, course_id,
                        owner_id=None, chapter_ids=None):
        calls.append((scope, owner_id))
        if scope == "personal":
            return [{"chunk": per_chunk, "score": 0.5}]
        return [{"chunk": pub_chunk, "score": 0.9}]

    monkeypatch.setattr(pipeline, "fine_search", fake_fine)
    session = FakeSession(_course(scope="public", owner_id=None))
    hits, chapter_ids = await pipeline.retrieve(session, "q", _user(7), 7, "mixed")
    assert chapter_ids is None
    assert calls == [("personal", 7), ("public", None)]
    assert [h["chunk"].chunk_id for h in hits] == ["per_d1_00001", "pub_d1_00001"]
    assert hits[0]["score"] > hits[1]["score"]


async def test_retrieve_raises_404_for_missing_course():
    """课程不存在：HTTPException 向上传播（由 answer_question 转 error 事件）。"""
    session = FakeSession(course=None)
    with pytest.raises(HTTPException):
        await pipeline.retrieve(session, "q", _user(7), 7, "personal")


# ---------------------------------------------------------------------------
# _build_citation_items：引用编号 → 课程/章节/页码/署名
# ---------------------------------------------------------------------------


async def test_build_citation_items_empty_input():
    session = FakeSession()
    assert await pipeline._build_citation_items(session, [], []) == []
    assert session.executes == 0  # 空引用不查库


async def test_build_citation_items_maps_metadata():
    chunk = _chunk()
    doc = SimpleNamespace(id=5, file_name="操作系统.pdf")
    rows = [(chunk, doc, "小明", "第三章 内存管理")]
    session = FakeSession(citation_rows=rows)
    items = await pipeline._build_citation_items(
        session, ["per_d1_00001", "per_d9_99999"], [{"chunk": chunk, "score": 0.1}]
    )
    assert len(items) == 1  # 未命中行的引用被丢弃
    item = items[0]
    assert item["chunk_id"] == "per_d1_00001"
    assert item["document_id"] == 5
    assert item["file_name"] == "操作系统.pdf"
    assert item["chapter"] == "第三章 内存管理"
    assert item["section"] == "3.1 分页存储"
    assert item["page_num"] == 12
    assert item["source_scope"] == "personal"
    assert item["uploader"] == "小明"
    assert item["snippet"] == chunk.content[:80]


# ---------------------------------------------------------------------------
# answer_question：SSE 事件流编排
# ---------------------------------------------------------------------------


def _patch_retrieve(monkeypatch, hits, coarse_ids):
    async def fake_retrieve(session, question, user, course_id, scope):
        return hits, coarse_ids

    monkeypatch.setattr(pipeline, "retrieve", fake_retrieve)


def _patch_quota(monkeypatch, consumed):
    async def fake_consume(session, user):
        consumed.append(user.id)

    monkeypatch.setattr(pipeline, "consume_quota", fake_consume)


async def test_answer_question_full_flow(monkeypatch):
    """完整链路：token → citations → done，qa_logs 落库并扣减官方额度。"""
    chunk = _chunk()
    _patch_retrieve(monkeypatch, [{"chunk": chunk, "score": 0.02}], [3, 4])
    consumed = []
    _patch_quota(monkeypatch, consumed)
    doc = SimpleNamespace(id=5, file_name="操作系统.pdf")
    session = FakeSession(citation_rows=[(chunk, doc, "小明", "第三章")])
    client = FakeLLMClient(
        ["分页是定长划分", " [per_d1_00001]。"], usage={"total_tokens": 42}
    )
    events = [e async for e in pipeline.answer_question(session, _user(7), _body(), client=client)]
    assert [e["type"] for e in events] == ["token", "token", "citations", "done"]
    assert events[0]["text"] == "分页是定长划分"
    # citations：引用映射到真实 chunk 元数据
    assert events[2]["items"][0]["chunk_id"] == "per_d1_00001"
    assert events[2]["items"][0]["uploader"] == "小明"
    # done：usage 与延迟
    assert events[3]["token_usage"] == 42
    assert isinstance(events[3]["latency_ms"], int)
    # qa_logs 落库字段
    log = session.added[0]
    assert isinstance(log, QaLog)
    assert log.user_id == 7 and log.course_id == 7 and log.search_scope == "personal"
    assert log.coarse_chapters == {"chapter_ids": [3, 4]}
    assert log.top_chunks == ["per_d1_00001"]
    assert log.answer == "分页是定长划分 [per_d1_00001]。"
    assert log.model_provider == "official"
    assert log.token_usage == 42
    assert "什么是分页存储" in log.prompt
    assert "per_d1_00001" in log.prompt  # 上下文带入 chunk_id 供模型引用
    assert consumed == [7] and session.commits == 1
    # 官方 SHORT 档客户端：chat_stream 收到 system prompt
    assert client.messages[0]["role"] == "system"


async def test_answer_question_user_custom_skips_quota(monkeypatch):
    """provider=user_custom：不占官方额度；粗召回 None 时 coarse_chapters 落 NULL。"""
    chunk = _chunk()
    _patch_retrieve(monkeypatch, [{"chunk": chunk, "score": 0.02}], None)
    consumed = []
    _patch_quota(monkeypatch, consumed)
    session = FakeSession()
    client = FakeLLMClient(["没有引用的回答。"], usage=None)  # usage 缺失按 0
    events = [
        e async for e in pipeline.answer_question(
            session, _user(7), _body(), client=client, provider="user_custom"
        )
    ]
    assert [e["type"] for e in events] == ["token", "citations", "done"]
    assert events[1]["items"] == []  # 无引用
    assert events[2]["token_usage"] == 0
    log = session.added[0]
    assert log.coarse_chapters is None
    assert log.model_provider == "user_custom"
    assert consumed == []  # 自定义 Key 不扣官方额度
    assert session.executes == 0  # 空引用不查库


async def test_answer_question_default_client_uses_official_short(monkeypatch):
    """client=None 时走官方 SHORT 档模型。"""
    _patch_retrieve(monkeypatch, [], None)
    _patch_quota(monkeypatch, [])
    tiers = []

    def fake_official(tier):
        tiers.append(tier)
        return FakeLLMClient(["空结果回答。"], usage={"total_tokens": 3})

    monkeypatch.setattr(pipeline, "get_official_client", fake_official)
    session = FakeSession()
    events = [e async for e in pipeline.answer_question(session, _user(7), _body())]
    assert tiers == [ModelTier.SHORT]
    assert events[-1]["type"] == "done"
    log = session.added[0]
    assert log.top_chunks == []
    assert "（未检索到相关资料）" in log.prompt  # 空命中兜底上下文


async def test_answer_question_retrieve_http_error(monkeypatch):
    """课程越权 404：yield error 事件（detail 透传）后结束，不落库。"""

    async def fake_retrieve(session, question, user, course_id, scope):
        raise HTTPException(status_code=404, detail="Not Found")

    monkeypatch.setattr(pipeline, "retrieve", fake_retrieve)
    session = FakeSession()
    events = [e async for e in pipeline.answer_question(session, _user(7), _body())]
    assert events == [{"type": "error", "message": "Not Found"}]
    assert session.added == [] and session.commits == 0


async def test_answer_question_retrieve_generic_error(monkeypatch):
    """检索服务异常：yield 兜底 error 文案，不暴露内部异常。"""

    async def fake_retrieve(session, question, user, course_id, scope):
        raise RuntimeError("chroma down")

    monkeypatch.setattr(pipeline, "retrieve", fake_retrieve)
    session = FakeSession()
    events = [e async for e in pipeline.answer_question(session, _user(7), _body())]
    assert events == [{"type": "error", "message": "检索服务暂时不可用，请稍后重试"}]


async def test_answer_question_llm_stream_failure(monkeypatch):
    """LLM 流式生成失败：yield error 后结束，不写 qa_logs、不扣额度。"""
    _patch_retrieve(monkeypatch, [{"chunk": _chunk(), "score": 0.02}], [3])
    consumed = []
    _patch_quota(monkeypatch, consumed)
    session = FakeSession()
    client = FakeLLMClient(fail=True)
    events = [e async for e in pipeline.answer_question(session, _user(7), _body(), client=client)]
    assert events == [{"type": "error", "message": "AI 服务暂时不可用，请稍后重试"}]
    assert session.added == [] and consumed == [] and session.commits == 0


async def test_answer_question_log_failure_still_done(monkeypatch):
    """qa_logs 落库失败：rollback 后仍 yield done（回答已产出，不因日志失败丢弃）。"""
    _patch_retrieve(monkeypatch, [{"chunk": _chunk(), "score": 0.02}], None)
    _patch_quota(monkeypatch, [])
    session = FakeSession(fail_commit=True)
    client = FakeLLMClient(["回答。"], usage={"total_tokens": 1})
    events = [e async for e in pipeline.answer_question(session, _user(7), _body(), client=client)]
    assert events[-1]["type"] == "done"
    assert session.commits == 0 and session.rollbacks == 1
