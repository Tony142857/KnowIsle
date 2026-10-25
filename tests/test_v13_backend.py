"""v1.3 B1~B5：失败恢复、并发幂等、状态权限与迁移契约。"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.api import admin as admin_api
from app.api import courses as courses_api
from app.api import documents as documents_api
from app.api import posts as posts_api
from app.api.resources import _office_preview_key
from app.community.experience import validate_experience_content
from app.community.posts import ai_answer_key, exp_summary_key
from app.identity.redirects import safe_next_path
from app.moderation.workflow import create_submission
from app.storage.models import (
    Course,
    Document,
    Notification,
    Post,
    QaLog,
    Resource,
    ReviewTask,
    User,
)
from app.workers import parse_worker, preview_worker, review_timeout_worker, review_worker

from .fakestack import (
    FakeArqPool,
    FakeRedis,
    FakeResult,
    FakeSession,
    async_value,
    http_client,
    make_course,
    make_doc,
    make_post,
    make_resource,
    make_task,
    make_user,
    session_local,
)

_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic/versions/c7e91a4f2b63_v13_error_evidence_metrics.py"
)
_SPEC = importlib.util.spec_from_file_location("v13_migration", _MIGRATION_PATH)
migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migration)

USER = make_user(1)
ADMIN = make_user(1, role="admin")


def test_safe_next_path_accepts_only_same_site_single_slash():
    assert safe_next_path(None) == "/"
    assert safe_next_path("/courses/1?tab=resources#top") == "/courses/1?tab=resources#top"
    for value in ("//evil.example", "https://evil.example", "/\\evil", "\\evil", "evil"):
        with pytest.raises(ValueError):
            safe_next_path(value)


def test_login_page_uses_safe_next_and_rejects_unsafe():
    with http_client(FakeSession(), USER) as client:
        resp = client.get("/login", params={"next": "/courses/1"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/courses/1"
        resp = client.get("/login", params={"next": "//evil.example"})
        assert resp.status_code == 422


def test_experience_background_is_enforced_at_http_boundary(monkeypatch):
    monkeypatch.setattr("app.api.posts.get_config", async_value(60))
    monkeypatch.setattr("app.api.posts.check_action_cooldown", async_value(None))
    with http_client(FakeSession(), USER) as client:
        resp = client.post(
            "/api/posts/", json={"board": "experience", "title": "经验", "content": "正文"}
        )
    assert resp.status_code == 422
    assert "背景" in resp.json()["detail"]
    validate_experience_content("## 时间线\n2026\n\n## 背景\n计算机专业")
    with pytest.raises(ValueError):
        validate_experience_content("## 背景\n  \n## 时间线\n2026")


def test_document_parse_error_is_safe_and_visible():
    doc = make_doc(11, status="failed", parse_error="文档解析失败，请稍后重试")
    session = FakeSession(gets={(Document, 11): doc}, results=[FakeResult(scalar=0)])
    with http_client(session, USER) as client:
        body = client.get("/api/documents/11/status").json()
    assert body["parse_error"] == "文档解析失败，请稍后重试"


async def test_parse_worker_persists_public_error_not_exception_details(monkeypatch):
    doc = make_doc(11, status="parsing", parse_error=None)
    session = FakeSession(gets={(Document, 11): doc})

    async def _fail(*_args):
        raise ValueError("文档解析结果为空: /private/secret.docx")

    monkeypatch.setattr(parse_worker, "SessionLocal", session_local(session))
    monkeypatch.setattr(parse_worker, "_run_pipeline", _fail)
    with pytest.raises(ValueError):
        await parse_worker.parse_document({"job_try": 3}, 11)
    assert doc.status == "failed"
    assert doc.parse_error == "文档中未提取到可用文本，请检查文件内容或更换格式后重试"
    assert "/private" not in doc.parse_error


def test_preview_retry_is_idempotent_and_owner_scoped(monkeypatch):
    pool = FakeArqPool()
    monkeypatch.setattr(documents_api, "get_arq_pool", async_value(pool))
    doc = make_doc(11, file_type="word", preview_key=None)
    with http_client(FakeSession(gets={(Document, 11): doc}), USER) as client:
        first = client.post("/api/documents/11/preview/retry")
        assert first.status_code == 200
        assert first.json()["preview_pending"] is True
        doc.preview_key = "preview/11/md5.pdf"
        second = client.post("/api/documents/11/preview/retry")
        assert second.json() == {
            "document_id": 11,
            "preview_pending": False,
            "queued": False,
        }
    assert pool.jobs == [("convert_preview", 11)]
    with http_client(FakeSession(gets={(Document, 11): doc}), make_user(2)) as client:
        assert client.post("/api/documents/11/preview/retry").status_code == 404


async def test_office_preview_prefers_personal_then_legacy_public():
    personal = make_doc(11, file_type="word", preview_key="preview/personal.pdf")
    assert await _office_preview_key(FakeSession(), personal) == "preview/personal.pdf"
    personal.preview_key = None
    public = make_doc(55, scope="public", preview_key="preview/public.pdf")
    session = FakeSession(results=[FakeResult(rows=[public])])
    assert await _office_preview_key(session, personal) == "preview/public.pdf"


def test_submit_office_enqueues_precheck_and_original_preview(monkeypatch):
    pool = FakeArqPool()
    monkeypatch.setattr(documents_api, "get_arq_pool", async_value(pool))
    monkeypatch.setattr(documents_api, "get_config", async_value(60))
    monkeypatch.setattr(documents_api, "check_action_cooldown", async_value(None))
    doc = make_doc(11, file_type="word", preview_key=None)
    resource = make_resource(review_status="pending")
    task = make_task(stage="precheck")
    monkeypatch.setattr(
        documents_api, "create_submission", async_value((resource, task, False))
    )
    session = FakeSession(gets={(Document, 11): doc, (Course, 5): make_course()})
    with http_client(session, USER) as client:
        resp = client.post(
            "/api/documents/11/submit", json={"title": "课件", "course_id": 5}
        )
    assert resp.status_code == 201
    assert resp.json()["preview_pending"] is True
    assert pool.jobs == [("precheck_submission", 41), ("convert_preview", 11)]


def test_submit_precheck_failure_is_recoverable(monkeypatch):
    pool = FakeArqPool(fail=True)
    monkeypatch.setattr(documents_api, "get_arq_pool", async_value(pool))
    monkeypatch.setattr(documents_api, "get_config", async_value(60))
    monkeypatch.setattr(documents_api, "check_action_cooldown", async_value(None))
    doc = make_doc(11, file_type="word", preview_key=None)
    resource = make_resource(review_status="pending")
    task = make_task(stage="precheck")
    monkeypatch.setattr(
        documents_api, "create_submission", async_value((resource, task, False))
    )
    session = FakeSession(gets={(Document, 11): doc, (Course, 5): make_course()})
    with http_client(session, USER) as client:
        resp = client.post(
            "/api/documents/11/submit", json={"title": "课件", "course_id": 5}
        )
        assert resp.status_code == 202
        assert resp.json()["review_status"] == "pending"
        assert resp.json()["precheck_queued"] is False
        assert task.precheck_result == {"error": "预检任务暂时无法启动，请稍后重试"}
        pool.fail = False
        retry_session = FakeSession(
            gets={(Document, 11): doc},
            results=[FakeResult(rows=[resource]), FakeResult(rows=[task])],
        )
        with http_client(retry_session, USER) as retry_client:
            retry = retry_client.post("/api/documents/11/submit/retry")
        assert retry.status_code == 200
        assert retry.json()["precheck_queued"] is True
    assert pool.jobs == [("precheck_submission", 41)]


def test_submit_retry_is_noop_after_stage_advanced(monkeypatch):
    pool = FakeArqPool()
    monkeypatch.setattr(documents_api, "get_arq_pool", async_value(pool))
    doc = make_doc(11)
    resource = make_resource(review_status="co_reviewing")
    task = make_task(stage="co_review")
    session = FakeSession(
        gets={(Document, 11): doc},
        results=[FakeResult(rows=[resource]), FakeResult(rows=[task])],
    )
    with http_client(session, USER) as client:
        resp = client.post("/api/documents/11/submit/retry")
    assert resp.status_code == 200
    assert resp.json()["review_status"] == "co_reviewing"
    assert resp.json()["queued"] is False
    assert pool.jobs == []


def test_final_side_effect_failure_keeps_approved_and_can_retry(monkeypatch):
    pool = FakeArqPool(fail=True)
    monkeypatch.setattr(admin_api, "get_arq_pool", async_value(pool))
    monkeypatch.setattr(admin_api, "final_verdict", async_value((20, 55)))
    task = make_task(41, stage="final")
    resource = make_resource(review_status="approved")
    new_doc = make_doc(55, scope="public", file_type="word", preview_key=None)
    session = FakeSession(
        gets={(ReviewTask, 41): task, (Resource, 21): resource, (Document, 55): new_doc}
    )
    with http_client(session, ADMIN) as client:
        resp = client.post(
            "/api/admin/review/final", json={"task_id": 41, "verdict": "approve"}
        )
    assert resp.status_code == 200
    assert resp.json()["review_status"] == "approved"
    assert resp.json()["queued"] == {"preview": False, "chapter_summaries": False}

    pool.fail = False
    task.stage = "done"
    src_doc = make_doc(11, file_type="word", preview_key=None)
    retry_session = FakeSession(
        gets={(ReviewTask, 41): task, (Resource, 21): resource, (Document, 11): src_doc},
        results=[FakeResult(rows=[new_doc])],
    )
    with http_client(retry_session, ADMIN) as client:
        retry = client.post("/api/admin/review/41/retry-side-effects")
    assert retry.status_code == 200
    assert retry.json()["review_status"] == "approved"
    assert retry.json()["queued"] == {"preview": True, "chapter_summaries": True}
    assert pool.jobs == [("convert_preview", 55), ("backfill_chapter_summaries", 5)]


class _LockRecordingSession(FakeSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.locks = []

    async def get(self, model, pk, **kwargs):
        self.locks.append((model, pk, kwargs))
        return await super().get(model, pk, **kwargs)


def test_final_and_direct_lock_task_before_verdict(monkeypatch):
    for route, workflow_name in (("final", "final_verdict"), ("direct", "direct_verdict")):
        monkeypatch.setattr(admin_api, workflow_name, async_value((0, None)))
        task = make_task(41, stage="final")
        session = _LockRecordingSession(gets={(ReviewTask, 41): task})
        with http_client(session, ADMIN) as client:
            resp = client.post(
                f"/api/admin/review/{route}",
                json={"task_id": 41, "verdict": "reject", "comment": "理由"},
            )
        assert resp.status_code == 200
        lock = next(item for item in session.locks if item[0] is ReviewTask)
        assert lock[2]["with_for_update"] is True
        assert lock[2]["populate_existing"] is True


class _UniqueConflictSession(FakeSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.conflict = True

    async def flush(self):
        if self.conflict:
            self.conflict = False
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        await super().flush()


def test_favorite_unique_conflict_is_idempotent_without_count_drift():
    resource = make_resource(fav_count=7)
    session = _UniqueConflictSession(
        gets={(Resource, 21): resource}, results=[FakeResult(rows=[])]
    )
    with http_client(session, USER) as client:
        resp = client.post(
            "/api/favorites/", json={"target_type": "resource", "target_id": 21}
        )
    assert resp.status_code == 200
    assert resp.json()["created"] is False
    assert resource.fav_count == 7
    assert session.commits == 1


def test_follow_unique_conflict_is_idempotent():
    course = make_course()
    session = _UniqueConflictSession(
        gets={(Course, 5): course}, results=[FakeResult(rows=[])]
    )
    with http_client(session, USER) as client:
        resp = client.post("/api/follows/", json={"target_type": "course", "target_id": 5})
    assert resp.status_code == 200
    assert resp.json()["created"] is False
    assert session.commits == 1


def test_course_leaderboard_matches_active_permission(monkeypatch):
    monkeypatch.setattr(courses_api, "get_redis", FakeRedis)
    monkeypatch.setattr(courses_api, "compute_course_scores", async_value({}))
    for status in ("pending", "disabled"):
        session = FakeSession(gets={(Course, 5): make_course(status=status)})
        with http_client(session, USER) as client:
            assert client.get("/api/courses/5/leaderboard").status_code == 404
        session = FakeSession(gets={(Course, 5): make_course(status=status)})
        with http_client(session, ADMIN) as client:
            assert client.get("/api/courses/5/leaderboard").status_code == 200


def test_library_documents_include_review_context():
    doc = make_doc(11)
    resource = make_resource(review_status="rejected")
    task = make_task(stage="done")
    record = SimpleNamespace(task_id=41, comment="内容不完整")
    session = FakeSession(
        gets={(Document, 11): doc},
        results=[
            FakeResult(rows=[doc]),
            FakeResult(rows=[resource]),
            FakeResult(rows=[task]),
            FakeResult(rows=[record]),
        ],
    )
    with http_client(session, USER) as client:
        body = client.get("/api/library/documents").json()
    assert body["items"] == [
        {
            "document_id": 11,
            "file_name": "笔记.pdf",
            "status": "parsed",
            "parse_error": None,
            "review_status": "rejected",
            "review_stage": "done",
            "review_reason": "内容不完整",
            "resource_id": 21,
            "resource_url": "/resources/21",
        }
    ]


def test_migration_nine_columns_and_reversible_operations(monkeypatch):
    assert migration.down_revision == "b4d8e2f60a17"
    assert Document.__table__.c.parse_error.nullable is True
    assert Post.__table__.c.ai_citation_chunk_ids.nullable is True
    assert QaLog.__table__.c.retrieval_metrics.nullable is True
    added = []
    dropped = []
    monkeypatch.setattr(migration.op, "add_column", lambda table, column: added.append((table, column)))
    monkeypatch.setattr(migration.op, "drop_column", lambda table, name: dropped.append((table, name)))
    migration.upgrade()
    assert [(table, column.name, column.nullable) for table, column in added] == [
        ("documents", "parse_error", True),
        ("posts", "ai_citation_chunk_ids", True),
        ("qa_logs", "retrieval_metrics", True),
    ]
    migration.downgrade()
    assert dropped == [
        ("qa_logs", "retrieval_metrics"),
        ("posts", "ai_citation_chunk_ids"),
        ("documents", "parse_error"),
    ]


def test_ai_answer_retry_is_owner_scoped_and_idempotent(monkeypatch):
    redis = FakeRedis()
    pool = FakeArqPool()
    monkeypatch.setattr(posts_api, "get_redis", lambda: redis)
    monkeypatch.setattr(posts_api, "get_arq_pool", async_value(pool))
    post = make_post(7, board="qa", author_id=1)
    with http_client(FakeSession(gets={(Post, 7): post}), USER) as client:
        first = client.post("/api/posts/7/ai-retry", json={"kind": "answer"})
        second = client.post("/api/posts/7/ai-retry", json={"kind": "answer"})
    assert first.json() == {
        "post_id": 7,
        "kind": "answer",
        "status": "pending",
        "queued": True,
    }
    assert second.json()["queued"] is False
    assert pool.jobs == [("generate_ai_first_answer", 7)]
    assert redis.kv[ai_answer_key(7)] == "pending"
    with http_client(FakeSession(gets={(Post, 7): post}), make_user(2)) as client:
        assert client.post(
            "/api/posts/7/ai-retry", json={"kind": "answer"}
        ).status_code == 404


def test_ai_summary_retry_admin_failure_recovery_and_done(monkeypatch):
    redis = FakeRedis()
    pool = FakeArqPool(fail=True)
    monkeypatch.setattr(posts_api, "get_redis", lambda: redis)
    monkeypatch.setattr(posts_api, "get_arq_pool", async_value(pool))
    post = make_post(7, board="experience", author_id=9)
    with http_client(FakeSession(gets={(Post, 7): post}), ADMIN) as client:
        failed = client.post("/api/posts/7/ai-retry", json={"kind": "summary"})
        assert failed.status_code == 503
        assert redis.kv[exp_summary_key(7)] == "failed"
        pool.fail = False
        retry = client.post("/api/posts/7/ai-retry", json={"kind": "summary"})
        assert retry.json()["queued"] is True
        post.ai_summary = "已完成"
        done = client.post("/api/posts/7/ai-retry", json={"kind": "summary"})
        assert done.json() == {
            "post_id": 7,
            "kind": "summary",
            "status": "done",
            "queued": False,
        }
        wrong_kind = client.post("/api/posts/7/ai-retry", json={"kind": "answer"})
        assert wrong_kind.status_code == 422
    assert pool.jobs == [("generate_experience_summary", 7)]


def test_get_post_passes_current_post_to_citation_builder(monkeypatch):
    post = make_post(7, board="qa", ai_first_answer="答案 [pub_d1_00001]")
    session = FakeSession(
        gets={(Post, 7): post, (User, 1): USER},
        results=[FakeResult(scalar=0), FakeResult(scalar=0)],
    )
    calls = []

    async def _citations(_db, answer, current_post):
        calls.append((answer, current_post))
        return []

    monkeypatch.setattr(posts_api, "build_ai_citations", _citations)
    with http_client(session, USER) as client:
        assert client.get("/api/posts/7").status_code == 200
    assert calls == [(post.ai_first_answer, post)]


def test_bounty_response_notification_links_to_comment(monkeypatch):
    monkeypatch.setattr(posts_api, "get_config", async_value(60))
    monkeypatch.setattr(posts_api, "check_action_cooldown", async_value(None))
    post = make_post(7, board="bounty", author_id=9)
    session = FakeSession(gets={(Post, 7): post})
    with http_client(session, USER) as client:
        response = client.post("/api/posts/7/comments", json={"content": "我有资料"})
    notification = next(obj for obj in session.added if isinstance(obj, Notification))
    assert response.status_code == 201
    assert notification.link == f"/posts/7#comment-{response.json()['comment_id']}"


async def test_create_submission_locks_document_before_duplicate_check():
    doc = make_doc(11)
    resource = make_resource(review_status="pending")
    session = _LockRecordingSession(
        gets={(Document, 11): doc}, results=[FakeResult(rows=[resource])]
    )
    with pytest.raises(ValueError, match="duplicate"):
        await create_submission(session, USER, doc, make_course(), None, "标题", None)
    lock = next(item for item in session.locks if item[0] is Document)
    assert lock[2] == {"with_for_update": True, "populate_existing": True}


async def test_precheck_worker_locks_and_skips_advanced_task(monkeypatch):
    task = make_task(41, stage="co_review")
    session = _LockRecordingSession(gets={(ReviewTask, 41): task})
    monkeypatch.setattr(review_worker, "SessionLocal", session_local(session))

    async def _unexpected(*_args):
        raise AssertionError("已推进任务不得重复预检")

    monkeypatch.setattr(review_worker, "run_precheck", _unexpected)
    await review_worker.precheck_submission({}, 41)
    assert session.commits == 0
    lock = next(item for item in session.locks if item[0] is ReviewTask)
    assert lock[2]["with_for_update"] is True


async def test_preview_worker_locks_and_skips_existing_preview(monkeypatch):
    doc = make_doc(11, file_type="word", preview_key="preview/11/existing.pdf")
    session = _LockRecordingSession(gets={(Document, 11): doc})
    monkeypatch.setattr(preview_worker, "SessionLocal", session_local(session))
    await preview_worker.convert_preview({}, 11)
    assert session.commits == 0
    lock = next(item for item in session.locks if item[0] is Document)
    assert lock[2]["with_for_update"] is True


def test_reassign_locks_task_before_stage_check():
    task = make_task(41, stage="final")
    session = _LockRecordingSession(gets={(ReviewTask, 41): task})
    with http_client(session, ADMIN) as client:
        response = client.post("/api/admin/review/reassign", json={"task_id": 41})
    assert response.status_code == 422
    lock = next(item for item in session.locks if item[0] is ReviewTask)
    assert lock[2] == {"with_for_update": True, "populate_existing": True}


async def test_timeout_scan_selects_overdue_tasks_for_update(monkeypatch):
    class _RecordingSession(FakeSession):
        def __init__(self):
            super().__init__(results=[FakeResult(rows=[])])
            self.statements = []

        async def execute(self, statement, *args, **kwargs):
            self.statements.append(str(statement))
            return await super().execute(statement, *args, **kwargs)

    session = _RecordingSession()
    monkeypatch.setattr(review_timeout_worker, "SessionLocal", session_local(session))
    monkeypatch.setattr(review_timeout_worker, "get_config", async_value(48))
    result = await review_timeout_worker.review_timeout_scan({})
    assert result == {"rescanned": 0, "reassigned": 0, "escalated": 0}
    assert any("FOR UPDATE" in statement for statement in session.statements)


def test_upload_dedup_query_is_personal_scoped_and_failure_has_safe_error(monkeypatch):
    class _RecordingSession(FakeSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.statements = []

        async def execute(self, statement, *args, **kwargs):
            self.statements.append(str(statement))
            return await super().execute(statement, *args, **kwargs)

    pool = FakeArqPool(fail=True)
    monkeypatch.setattr(documents_api, "get_arq_pool", async_value(pool))
    monkeypatch.setattr(documents_api, "get_config", async_value(60))
    monkeypatch.setattr(documents_api, "check_action_cooldown", async_value(None))
    monkeypatch.setattr(documents_api.object_store, "put_object", async_value(None))
    course = make_course(scope="personal", owner_id=1)
    session = _RecordingSession(
        gets={(Course, 5): course}, results=[FakeResult(rows=[])]
    )
    with http_client(session, USER) as client:
        response = client.post(
            "/api/documents/",
            files={"file": ("笔记.pdf", b"pdf", "application/pdf")},
            data={"course_id": "5"},
        )
    doc = next(obj for obj in session.added if isinstance(obj, Document))
    assert response.status_code == 500
    assert doc.status == "failed"
    assert doc.parse_error == "解析任务暂时无法启动，请稍后重传"
    assert any("documents.scope" in statement for statement in session.statements)
