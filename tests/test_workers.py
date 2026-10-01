"""v0.9 workers 覆盖测试（§18.1）：直接调用 ARQ 任务函数，Fake ctx/Session + monkeypatch 边界。

覆盖 parse / review / summary / ai_answer / settle / preview / experience_summary /
review_timeout 八个 worker 的主流程与关键分支（幂等早退、失败重试、末次容错降级）；
对象存储 / Chroma / LLM / LibreOffice / Redis 全部为内存替身，见 tests/fakestack.py。
"""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import pipeline
from app.identity import quota
from app.storage.models import (
    AiQuota,
    Chunk,
    Document,
    Notification,
    Post,
    QaLog,
    Resource,
    ReviewTask,
    User,
)
from app.workers import (
    ai_answer_worker,
    experience_summary_worker,
    parse_worker,
    preview_worker,
    review_timeout_worker,
    review_worker,
    settle_worker,
    summary_worker,
)
from app.workers.parse_worker import build_chunk_meta, parse_document
from app.workers.review_timeout_worker import review_timeout_scan
from app.workers.review_worker import precheck_submission
from app.workers.settle_worker import settle_scores
from app.workers.summary_worker import backfill_chapter_summaries

from .fakestack import (
    FakeEmbedding,
    FakeLLM,
    FakeObjectStore,
    FakeRedis,
    FakeResult,
    FakeSession,
    FakeVectorStore,
    async_value,
    make_doc,
    make_post,
    make_resource,
    make_task,
    make_user,
    session_local,
)


def _doc(**kw):
    return make_doc(doc_id=5, status="parsing", **kw)


def _chunks():
    return [
        {"chunk_id": "pub_d5_00001", "chapter_id": None, "page_num": 3,
         "section": "1.1", "content": "第一段"},
        {"chunk_id": "pub_d5_00002", "chapter_id": 10, "page_num": 4,
         "section": "1.2", "content": "第二段"},
    ]


def _patch_parse_pipeline(monkeypatch, session, blocks=None, chunks=None):
    """解析流水线全部外部边界换成内存替身，返回 (store, vector_store, bm25 记录)。"""
    store = FakeObjectStore({"k/5": b"pdf-bytes"})
    vector_store = FakeVectorStore()
    invalidated = []
    monkeypatch.setattr(parse_worker, "SessionLocal", session_local(session))
    monkeypatch.setattr(parse_worker, "object_store", store)
    monkeypatch.setattr(parse_worker, "parse_by_file_type",
                        lambda _ft, _data: blocks if blocks is not None else [{"b": 1}])
    monkeypatch.setattr(parse_worker, "tree_builder",
                        SimpleNamespace(build_tree=async_value({1: 10})))
    monkeypatch.setattr(parse_worker, "semantic_splitter",
                        SimpleNamespace(split_blocks=lambda *_a: chunks if chunks is not None else _chunks()))
    monkeypatch.setattr(parse_worker, "get_embedding", FakeEmbedding)
    monkeypatch.setattr(parse_worker, "get_vector_store", lambda: vector_store)
    monkeypatch.setattr(parse_worker, "invalidate_bm25",
                        lambda *args: invalidated.append(args))
    return store, vector_store, invalidated


# ---------------------------------------------------------------------------
# parse_worker：解析 → 切块 → 向量化 → 落库
# ---------------------------------------------------------------------------


def test_build_chunk_meta_sentinels():
    # chapter_id/page_num 可空字段用 -1 哨兵；有值时透传
    doc = _doc()
    meta = build_chunk_meta(doc, {"chunk_id": "c1", "chapter_id": None, "page_num": None})
    assert meta == {
        "chunk_id": "c1", "document_id": 5, "course_id": 5, "owner_id": 1,
        "chapter_id": -1, "page_num": -1, "file_type": "pdf_textbook",
    }
    meta = build_chunk_meta(doc, {"chunk_id": "c2", "chapter_id": 10, "page_num": 7})
    assert meta["chapter_id"] == 10 and meta["page_num"] == 7


async def test_parse_document_success_public(monkeypatch):
    doc = _doc(scope="public")
    session = FakeSession(gets={(Document, 5): doc}, results=[FakeResult()])
    _, vector_store, invalidated = _patch_parse_pipeline(monkeypatch, session)
    await parse_document({}, 5)
    assert doc.status == "parsed"
    assert session.commits == 1
    chunks = [o for o in session.added if isinstance(o, Chunk)]
    assert [c.chunk_id for c in chunks] == ["pub_d5_00001", "pub_d5_00002"]
    # public 文档写入公共 Collection；落库后失效该课程 BM25 缓存
    assert vector_store.upserts[0][0] == "chunks_public"
    assert vector_store.upserts[0][1]["ids"] == ["pub_d5_00001", "pub_d5_00002"]
    assert invalidated == [(5, "public", 1)]


async def test_parse_document_success_personal_collection(monkeypatch):
    doc = _doc(scope="personal")
    session = FakeSession(gets={(Document, 5): doc}, results=[FakeResult()])
    _, vector_store, _ = _patch_parse_pipeline(monkeypatch, session)
    await parse_document({}, 5)
    assert vector_store.upserts[0][0] == "chunks_personal"


async def test_parse_document_missing_doc_is_noop(monkeypatch):
    session = FakeSession()
    _patch_parse_pipeline(monkeypatch, session)
    await parse_document({}, 999)  # 文档不存在：记 warning 直接返回
    assert session.commits == 0


async def test_parse_document_empty_blocks_retryable(monkeypatch):
    doc = _doc()
    session = FakeSession(gets={(Document, 5): doc})
    _patch_parse_pipeline(monkeypatch, session, blocks=[])
    with pytest.raises(ValueError, match="解析结果为空"):
        await parse_document({"job_try": 1}, 5)
    assert session.rollbacks == 1 and session.commits == 0
    assert doc.status == "parsing"  # 非末次重试：不标记 failed，交 arq 重试


async def test_parse_document_last_try_marks_failed(monkeypatch):
    doc = _doc()
    session = FakeSession(gets={(Document, 5): doc})
    _patch_parse_pipeline(monkeypatch, session, blocks=[])
    with pytest.raises(ValueError):
        await parse_document({"job_try": 3}, 5)
    assert doc.status == "failed"  # 末次重试仍失败：标记 failed 并提交后再抛出
    assert session.commits == 1


async def test_parse_document_empty_chunks_fails(monkeypatch):
    doc = _doc()
    session = FakeSession(gets={(Document, 5): doc})
    _patch_parse_pipeline(monkeypatch, session, chunks=[])
    with pytest.raises(ValueError, match="切块结果为空"):
        await parse_document({"job_try": 1}, 5)


# ---------------------------------------------------------------------------
# review_worker：投稿自动预检
# ---------------------------------------------------------------------------


def _patch_review_session(monkeypatch, session):
    monkeypatch.setattr(review_worker, "SessionLocal", session_local(session))


async def test_precheck_missing_task_or_resource(monkeypatch):
    task = make_task(stage="precheck")
    session = FakeSession()  # task 不存在
    _patch_review_session(monkeypatch, session)
    await precheck_submission({}, 41)
    session = FakeSession(gets={(ReviewTask, 41): task})  # resource 不存在
    _patch_review_session(monkeypatch, session)
    await precheck_submission({}, 41)
    assert session.commits == 0


async def test_precheck_success(monkeypatch):
    task, resource = make_task(stage="precheck"), make_resource()
    session = FakeSession(gets={(ReviewTask, 41): task, (Resource, 21): resource})
    _patch_review_session(monkeypatch, session)
    applied = []
    monkeypatch.setattr(review_worker, "run_precheck", async_value({"hard_fail": False}))

    async def _apply(_session, _task, result):
        applied.append(result)

    monkeypatch.setattr(review_worker, "apply_precheck_result", _apply)
    await precheck_submission({}, 41)
    assert applied == [{"hard_fail": False}]
    assert session.commits == 1


async def test_precheck_failure_retryable(monkeypatch):
    task, resource = make_task(stage="precheck"), make_resource()
    session = FakeSession(gets={(ReviewTask, 41): task, (Resource, 21): resource})
    _patch_review_session(monkeypatch, session)

    async def _boom(_session, _resource):
        raise RuntimeError("预检服务异常")

    monkeypatch.setattr(review_worker, "run_precheck", _boom)
    with pytest.raises(RuntimeError):
        await precheck_submission({"job_try": 1}, 41)
    assert session.rollbacks == 1 and session.commits == 0


async def test_precheck_last_try_falls_back_to_manual(monkeypatch):
    task, resource = make_task(stage="precheck"), make_resource()
    session = FakeSession(gets={(ReviewTask, 41): task, (Resource, 21): resource})
    _patch_review_session(monkeypatch, session)
    applied = []

    async def _boom(_session, _resource):
        raise RuntimeError("预检服务异常")

    async def _apply(_session, _task, result):
        applied.append(result)

    monkeypatch.setattr(review_worker, "run_precheck", _boom)
    monkeypatch.setattr(review_worker, "apply_precheck_result", _apply)
    with pytest.raises(RuntimeError):
        await precheck_submission({"job_try": 3}, 41)
    # 末次重试仍失败：容错推进人工协审（fallback 标记 error），提交后再抛出
    assert applied[0]["error"] == "预检异常，转人工协审"
    assert applied[0]["hard_fail"] is False
    assert session.commits == 1


# ---------------------------------------------------------------------------
# summary_worker：章节摘要回填
# ---------------------------------------------------------------------------


def _chapter(chapter_id):
    return SimpleNamespace(id=chapter_id, course_id=5, title=f"第 {chapter_id} 章",
                           summary_vector_id=None)


def _patch_summary(monkeypatch, session, llm):
    vector_store = FakeVectorStore()
    monkeypatch.setattr(summary_worker, "SessionLocal", session_local(session))
    monkeypatch.setattr(summary_worker, "get_vector_store", lambda: vector_store)
    monkeypatch.setattr(summary_worker, "get_embedding", FakeEmbedding)
    monkeypatch.setattr(summary_worker, "get_official_client", lambda _tier: llm)
    return vector_store


async def test_backfill_no_chapters(monkeypatch):
    session = FakeSession(results=[FakeResult(rows=[])])
    _patch_summary(monkeypatch, session, FakeLLM())
    await backfill_chapter_summaries({}, 5)
    assert session.commits == 1


async def test_backfill_success_and_skip_empty(monkeypatch):
    ch1, ch2 = _chapter(1), _chapter(2)
    session = FakeSession(results=[
        FakeResult(rows=[ch1, ch2]),                       # 章节列表
        FakeResult(rows=[SimpleNamespace(content="正文一")]),  # ch1 的 public chunks
        FakeResult(rows=[]),                               # ch2 无 public 内容：跳过
    ])
    vector_store = _patch_summary(monkeypatch, session, FakeLLM("章节摘要"))
    await backfill_chapter_summaries({}, 5)
    assert ch1.summary_vector_id == "ch_1"
    assert ch2.summary_vector_id is None
    assert vector_store.upserts[0][1]["ids"] == ["ch_1"]
    assert vector_store.upserts[0][1]["metadatas"] == [{"course_id": 5}]
    assert session.commits == 1


async def test_backfill_llm_failure_skips_chapter(monkeypatch):
    ch1, ch2 = _chapter(1), _chapter(2)
    session = FakeSession(results=[
        FakeResult(rows=[ch1, ch2]),
        FakeResult(rows=[SimpleNamespace(content="正文一")]),
        FakeResult(rows=[SimpleNamespace(content="正文二")]),
    ])
    # 第一章 LLM 失败仅记 warning 跳过，第二章不受影响
    calls = {"n": 0}

    async def _chat(messages):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("LLM 超时")
        return "摘要", {"tokens": 1}

    vector_store = _patch_summary(monkeypatch, session, FakeLLM())
    monkeypatch.setattr(summary_worker, "get_official_client",
                        lambda _tier: SimpleNamespace(chat=_chat))
    await backfill_chapter_summaries({}, 5)
    assert ch1.summary_vector_id is None
    assert ch2.summary_vector_id == "ch_2"
    assert len(vector_store.upserts) == 1


# ---------------------------------------------------------------------------
# ai_answer_worker / experience_summary_worker：AI 首答 / 经验帖摘要
# ---------------------------------------------------------------------------


def _patch_ai_answer(monkeypatch, session, redis, llm, hits=None, retrieve_fail=False):
    monkeypatch.setattr(ai_answer_worker, "SessionLocal", session_local(session))
    monkeypatch.setattr(ai_answer_worker, "get_redis", lambda: redis)
    monkeypatch.setattr(ai_answer_worker, "get_official_client", lambda _tier: llm)

    async def _retrieve(_session, _question, _author, _course_id, _scope):
        if retrieve_fail:
            raise RuntimeError("检索服务异常")
        return hits or [], None

    monkeypatch.setattr(ai_answer_worker, "retrieve", _retrieve)


async def test_ai_answer_early_return_cleans_pending_key(monkeypatch):
    redis = FakeRedis()
    key = ai_answer_worker.ai_answer_key(7)
    redis.kv[key] = "pending"
    author = make_user(9)
    cases = [
        {},  # 帖子不存在
        {(Post, 7): make_post(7, board="discuss")},  # 非 qa 帖
        {(Post, 7): make_post(7, ai_first_answer="已有答案")},  # 已生成
    ]
    for gets in cases:
        redis.kv[key] = "pending"
        session = FakeSession(gets={**gets, (User, 9): author})
        _patch_ai_answer(monkeypatch, session, redis, FakeLLM())
        await ai_answer_worker.generate_ai_first_answer({}, 7)
        assert key not in redis.kv  # 早退路径清理 pending 键


async def test_ai_answer_success(monkeypatch):
    redis = FakeRedis()
    key = ai_answer_worker.ai_answer_key(7)
    redis.kv[key] = "pending"
    post = make_post(7, author_id=9, board="qa")
    session = FakeSession(gets={(Post, 7): post, (User, 9): make_user(9)})
    hits = [{"chunk": SimpleNamespace(chunk_id="pub_d1_00001", content="参考资料")}]
    _patch_ai_answer(monkeypatch, session, redis, FakeLLM("AI 答案"), hits=hits)
    await ai_answer_worker.generate_ai_first_answer({}, 7)
    assert post.ai_first_answer == "AI 答案"
    assert key not in redis.kv
    notifications = [o for o in session.added if isinstance(o, Notification)]
    assert len(notifications) == 1
    assert notifications[0].type == "ai_answer" and notifications[0].user_id == 9
    assert session.commits == 1


async def test_ai_answer_failure_retry_then_failed_state(monkeypatch):
    redis = FakeRedis()
    key = ai_answer_worker.ai_answer_key(7)
    post = make_post(7, author_id=9, board="qa")
    session = FakeSession(gets={(Post, 7): post, (User, 9): make_user(9)})
    _patch_ai_answer(monkeypatch, session, redis, FakeLLM(), retrieve_fail=True)
    with pytest.raises(RuntimeError):
        await ai_answer_worker.generate_ai_first_answer({"job_try": 1}, 7)
    assert session.rollbacks == 1
    # 末次重试：置 Redis failed 状态后吞掉异常（交 arq 终结，不再重试）
    await ai_answer_worker.generate_ai_first_answer({"job_try": 3}, 7)
    assert redis.kv[key] == "failed"


def _patch_exp_summary(monkeypatch, session, redis, llm):
    monkeypatch.setattr(experience_summary_worker, "SessionLocal", session_local(session))
    monkeypatch.setattr(experience_summary_worker, "get_redis", lambda: redis)
    monkeypatch.setattr(experience_summary_worker, "get_official_client", lambda _tier: llm)


async def test_exp_summary_early_return_cleans_key(monkeypatch):
    redis = FakeRedis()
    key = experience_summary_worker.exp_summary_key(7)
    cases = [
        {},
        {(Post, 7): make_post(7, board="qa")},
        {(Post, 7): make_post(7, board="experience", ai_summary="已有摘要")},
    ]
    for gets in cases:
        redis.kv[key] = "pending"
        session = FakeSession(gets=gets)
        _patch_exp_summary(monkeypatch, session, redis, FakeLLM())
        await experience_summary_worker.generate_experience_summary({}, 7)
        assert key not in redis.kv


async def test_exp_summary_success(monkeypatch):
    redis = FakeRedis()
    key = experience_summary_worker.exp_summary_key(7)
    redis.kv[key] = "pending"
    post = make_post(7, author_id=9, board="experience")
    session = FakeSession(gets={(Post, 7): post})
    llm = FakeLLM("经验摘要")
    _patch_exp_summary(monkeypatch, session, redis, llm)
    await experience_summary_worker.generate_experience_summary({}, 7)
    assert post.ai_summary == "经验摘要"
    assert "测试帖子" in llm.prompts[0]  # 输入为「标题 + 正文前 2000 字」
    assert key not in redis.kv
    notifications = [o for o in session.added if isinstance(o, Notification)]
    assert len(notifications) == 1 and notifications[0].type == "ai_summary"


async def test_exp_summary_failure_retry_then_failed_state(monkeypatch):
    redis = FakeRedis()
    key = experience_summary_worker.exp_summary_key(7)
    post = make_post(7, author_id=9, board="experience")
    session = FakeSession(gets={(Post, 7): post})
    _patch_exp_summary(monkeypatch, session, redis, FakeLLM(fail=True))
    with pytest.raises(RuntimeError):
        await experience_summary_worker.generate_experience_summary({"job_try": 1}, 7)
    await experience_summary_worker.generate_experience_summary({"job_try": 3}, 7)
    assert redis.kv[key] == "failed"


# ---------------------------------------------------------------------------
# settle_worker：贡献榜每日对账
# ---------------------------------------------------------------------------


async def test_settle_rebuilds_major_and_course_boards(monkeypatch):
    session = FakeSession(results=[FakeResult(rows=[(3, 7, 60), (3, 8, 50), (4, 9, 40)])])
    monkeypatch.setattr(settle_worker, "SessionLocal", session_local(session))
    monkeypatch.setattr(settle_worker, "compute_course_scores",
                        async_value({5: {7: 30, 8: 0}, 6: {9: 10}}))
    redis = FakeRedis()
    monkeypatch.setattr(settle_worker, "get_redis", lambda: redis)
    summary = await settle_scores({})
    assert summary == {"majors": 2, "courses": 2}
    assert redis.zsets["rank:major:3"] == {"7": 60, "8": 50}
    assert redis.zsets["rank:course:5"] == {"7": 30}  # 0 分成员被过滤
    assert redis.zsets["rank:course:6"] == {"9": 10}


async def test_settle_tolerates_single_key_failure(monkeypatch):
    session = FakeSession(results=[FakeResult(rows=[(3, 7, 60), (4, 9, 40)])])
    monkeypatch.setattr(settle_worker, "SessionLocal", session_local(session))
    monkeypatch.setattr(settle_worker, "compute_course_scores", async_value({}))

    class _FlakyRedis(FakeRedis):
        async def delete(self, *keys):
            if keys[0] == "rank:major:3":
                raise ConnectionError("redis down")
            await super().delete(*keys)

    redis = _FlakyRedis()
    monkeypatch.setattr(settle_worker, "get_redis", lambda: redis)
    summary = await settle_scores({})
    # 单键失败不中断整体对账：major 3 重建失败，major 4 照常
    assert summary == {"majors": 1, "courses": 0}
    assert redis.zsets["rank:major:4"] == {"9": 40}


# ---------------------------------------------------------------------------
# preview_worker：Office → PDF 预览转换（soffice 以假子进程替代）
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode=0):
        self.returncode = returncode

    async def communicate(self):
        return (b"", b"")


def _fake_soffice(monkeypatch, returncode=0, write_pdf=True):
    async def _exec(*args):
        if write_pdf:
            outdir = Path(args[5])  # ("soffice", "--headless", "--convert-to", "pdf", "--outdir", dir, src)
            (Path(outdir) / "source.pdf").write_bytes(b"pdf-bytes")
        return _FakeProc(returncode)

    monkeypatch.setattr(preview_worker.asyncio, "create_subprocess_exec", _exec)


async def test_convert_preview_skips_missing_and_non_office(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(preview_worker, "SessionLocal", session_local(session))
    await preview_worker.convert_preview({}, 999)  # 文档不存在
    session = FakeSession(gets={(Document, 5): _doc(file_type="markdown")})
    monkeypatch.setattr(preview_worker, "SessionLocal", session_local(session))
    await preview_worker.convert_preview({}, 5)  # 非 word/ppt 无需转换
    assert session.commits == 0


async def test_convert_preview_success(monkeypatch):
    doc = _doc(file_type="word", md5="m5")
    session = FakeSession(gets={(Document, 5): doc})
    store = FakeObjectStore({"k/5": b"docx-bytes"})
    monkeypatch.setattr(preview_worker, "SessionLocal", session_local(session))
    monkeypatch.setattr(preview_worker, "object_store", store)
    _fake_soffice(monkeypatch)
    await preview_worker.convert_preview({}, 5)
    assert doc.preview_key == "preview/5/m5.pdf"
    assert store.objects["preview/5/m5.pdf"] == b"pdf-bytes"
    assert session.commits == 1


async def test_convert_preview_soffice_failure(monkeypatch):
    doc = _doc(file_type="ppt")
    session = FakeSession(gets={(Document, 5): doc})
    store = FakeObjectStore({"k/5": b"pptx-bytes"})
    monkeypatch.setattr(preview_worker, "SessionLocal", session_local(session))
    monkeypatch.setattr(preview_worker, "object_store", store)
    _fake_soffice(monkeypatch, returncode=1, write_pdf=False)
    with pytest.raises(RuntimeError, match="预览转换失败"):
        await preview_worker.convert_preview({}, 5)
    assert doc.preview_key is None


# ---------------------------------------------------------------------------
# review_timeout_worker：协审超时自动重指派
# ---------------------------------------------------------------------------


def _patch_timeout(monkeypatch, session, assignee_ids):
    monkeypatch.setattr(review_timeout_worker, "SessionLocal", session_local(session))
    monkeypatch.setattr(review_timeout_worker, "get_config", async_value(48))
    monkeypatch.setattr(review_timeout_worker, "assign_reviewers", async_value(assignee_ids))


async def test_timeout_scan_no_overdue_tasks(monkeypatch):
    session = FakeSession(results=[FakeResult(rows=[])])
    _patch_timeout(monkeypatch, session, [])
    result = await review_timeout_scan({})
    assert result == {"rescanned": 0, "reassigned": 0, "escalated": 0}
    assert session.commits == 1


async def test_timeout_scan_reassigns_and_resets_clock(monkeypatch):
    task = make_task(assignee_ids=[3, 4], created_at=datetime(2026, 9, 1, tzinfo=UTC))
    resource = make_resource()
    session = FakeSession(
        gets={(Resource, 21): resource},
        results=[FakeResult(rows=[task]), FakeResult(rows=[3])],  # 协审员 3 已提交意见
    )
    _patch_timeout(monkeypatch, session, [11])
    result = await review_timeout_scan({})
    assert result == {"rescanned": 1, "reassigned": 1, "escalated": 0}
    # 已提交意见者保留，新指派补足到 2 人；created_at 重置重新计时
    assert task.assignee_ids == [3, 11]
    assert task.created_at != datetime(2026, 9, 1, tzinfo=UTC)
    notifications = [o for o in session.added if isinstance(o, Notification)]
    assert len(notifications) == 1
    assert notifications[0].type == "review_assign" and notifications[0].user_id == 11


async def test_timeout_scan_escalates_when_no_reviewer(monkeypatch):
    task = make_task(assignee_ids=[3, 4], created_at=datetime(2026, 9, 1, tzinfo=UTC))
    resource = make_resource()
    admin = make_user(50, role="admin")
    session = FakeSession(
        gets={(Resource, 21): resource},
        results=[
            FakeResult(rows=[task]),
            FakeResult(rows=[]),       # 无人提交意见
            FakeResult(rows=[admin]),  # 在职管理员
        ],
    )
    _patch_timeout(monkeypatch, session, [])  # 也找不到新协审员
    result = await review_timeout_scan({})
    assert result == {"rescanned": 1, "reassigned": 0, "escalated": 1}
    assert task.stage == "final"  # 兜底直送管理员终审
    notifications = [o for o in session.added if isinstance(o, Notification)]
    assert len(notifications) == 1
    assert notifications[0].type == "review_escalate" and notifications[0].user_id == 50


async def test_timeout_scan_skips_fully_voted_task(monkeypatch):
    task = make_task(assignee_ids=[3], created_at=datetime(2026, 9, 1, tzinfo=UTC))
    session = FakeSession(
        gets={(Resource, 21): make_resource()},
        results=[FakeResult(rows=[task]), FakeResult(rows=[3])],  # 唯一指派者已提交
    )
    _patch_timeout(monkeypatch, session, [11])
    result = await review_timeout_scan({})
    assert result == {"rescanned": 1, "reassigned": 0, "escalated": 0}
    assert not session.added


async def test_settle_course_board_skip_empty_and_tolerate_failure(monkeypatch):
    session = FakeSession(results=[FakeResult(rows=[])])  # 无专业榜数据
    monkeypatch.setattr(settle_worker, "SessionLocal", session_local(session))
    monkeypatch.setattr(settle_worker, "compute_course_scores",
                        async_value({5: {7: 0}, 6: {9: 10}}))

    class _FlakyRedis(FakeRedis):
        async def delete(self, *keys):
            if keys[0] == "rank:course:6":
                raise ConnectionError("redis down")
            await super().delete(*keys)

    redis = _FlakyRedis()
    monkeypatch.setattr(settle_worker, "get_redis", lambda: redis)
    summary = await settle_scores({})
    # 课程 5 全员 0 分跳过重建；课程 6 单键失败不中断对账
    assert summary == {"majors": 0, "courses": 0}
    assert "rank:course:5" not in redis.zsets


# ---------------------------------------------------------------------------
# v0.9 缺陷修复附属用例（quota 新建行 / pipeline 引用映射兜底，就近挂靠本文件）
# ---------------------------------------------------------------------------


async def test_ensure_quota_new_row_bonus_balance_zero(monkeypatch):
    """ai_daily_limit=0 且当日新建行：bonus_balance 显式为 0，check/consume 不对 None 做算术。"""
    monkeypatch.setattr(quota, "get_config", async_value(0))
    user = make_user(1)
    # check：免费额度为 0 且无余额 → 429，而非对 None 比较的 TypeError
    session = FakeSession(results=[FakeResult(scalar=None)])
    with pytest.raises(HTTPException) as exc_info:
        await quota.check_quota(session, user)
    assert exc_info.value.status_code == 429
    row = next(o for o in session.added if isinstance(o, AiQuota))
    assert (row.used, row.daily_limit, row.bonus_balance) == (0, 0, 0)
    # consume：新建行属性可直接算术，不炸
    session = FakeSession(results=[FakeResult(scalar=None)])
    await quota.consume_quota(session, user)
    row = next(o for o in session.added if isinstance(o, AiQuota))
    assert row.used == 0 and isinstance(row.bonus_balance, int)


async def test_answer_question_citation_mapping_failure_degrades(monkeypatch):
    """引用映射查询抛错（token 已流出）：降级为空 citations，仍收到 done 且 qa_logs 落库。"""

    async def _retrieve(*_args):
        return [], None

    async def _boom(*_args):
        raise RuntimeError("DB 不可用")

    monkeypatch.setattr(pipeline, "retrieve", _retrieve)
    monkeypatch.setattr(pipeline, "_build_citation_items", _boom)

    class _StreamClient:
        last_usage = {"total_tokens": 5}

        async def chat_stream(self, _messages):
            yield "答"
            yield "案"

    body = SimpleNamespace(question="什么是死锁", course_id=5, scope="public")
    session = FakeSession()
    events = [
        event
        async for event in pipeline.answer_question(
            session, make_user(1), body, client=_StreamClient(), provider="user_custom"
        )
    ]
    assert [e["type"] for e in events] == ["token", "token", "citations", "done"]
    assert events[2]["items"] == []
    assert len([o for o in session.added if isinstance(o, QaLog)]) == 1
