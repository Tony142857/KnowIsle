"""真实 PostgreSQL 并发验证；仅在显式 V13_PG_TEST=1 的隔离数据库容器运行。"""

import asyncio
import os
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import func, select

from app.api import posts as posts_api
from app.api.admin import FinalVerdictRequest, final_review
from app.api.favorites import FavoriteRequest, add_favorite
from app.api.follows import FollowRequest, add_follow
from app.api.posts import AiRetryRequest, retry_post_ai
from app.api.review_tasks import VerdictRequest, submit_verdict
from app.moderation import workflow
from app.moderation.workflow import create_submission
from app.storage.db import SessionLocal
from app.storage.models import (
    Chunk,
    Course,
    Document,
    Favorite,
    Follow,
    Major,
    Notification,
    Post,
    Resource,
    ReviewRecord,
    ReviewTask,
    ScoreLog,
    User,
)
from app.workers import review_timeout_worker, review_worker
from app.workers.review_timeout_worker import review_timeout_scan
from app.workers.review_worker import precheck_submission

from .fakestack import FakeArqPool, FakeRedis, async_value

pytestmark = pytest.mark.skipif(
    os.environ.get("V13_PG_TEST") != "1",
    reason="需要显式提供隔离 PostgreSQL 容器",
)


class _Embedding:
    async def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


class _VectorStore:
    async def get_or_create_collection(self, name):
        return name

    async def upsert(self, *_args, **_kwargs):
        return None


async def _fixtures() -> dict:
    async with SessionLocal() as session:
        major = Major(name="v13 并发测试")
        admin = User(student_no="v13-admin", real_name="管理员", nickname="v13-admin", role="admin")
        uploader = User(student_no="v13-uploader", real_name="上传者", nickname="v13-uploader")
        reviewer = User(
            student_no="v13-reviewer", real_name="协审员", nickname="v13-reviewer", role="reviewer"
        )
        reviewer2 = User(
            student_no="v13-reviewer2", real_name="协审员二", nickname="v13-reviewer2", role="reviewer"
        )
        reviewer3 = User(
            student_no="v13-reviewer3", real_name="协审员三", nickname="v13-reviewer3", role="reviewer"
        )
        session.add_all([major, admin, uploader, reviewer, reviewer2, reviewer3])
        await session.flush()
        personal_course = Course(scope="personal", owner_id=uploader.id, name="个人课")
        public_course = Course(scope="public", major_id=major.id, name="公共课")
        session.add_all([personal_course, public_course])
        await session.flush()
        doc = Document(
            course_id=personal_course.id,
            owner_id=uploader.id,
            scope="personal",
            file_name="notes.md",
            file_type="markdown",
            storage_key="v13/concurrency/source",
            md5="v13-concurrency-md5",
            status="parsed",
        )
        submission_doc = Document(
            course_id=personal_course.id,
            owner_id=uploader.id,
            scope="personal",
            file_name="submission.md",
            file_type="markdown",
            storage_key="v13/concurrency/submission",
            md5="v13-submission-md5",
            status="parsed",
        )
        timeout_doc = Document(
            course_id=personal_course.id,
            owner_id=uploader.id,
            scope="personal",
            file_name="timeout.md",
            file_type="markdown",
            storage_key="v13/concurrency/timeout",
            md5="v13-timeout-md5",
            status="parsed",
        )
        session.add_all([doc, submission_doc, timeout_doc])
        await session.flush()
        session.add(
            Chunk(
                chunk_id="per_d_v13_00001",
                document_id=doc.id,
                course_id=personal_course.id,
                owner_id=uploader.id,
                scope="personal",
                file_type="markdown",
                content="并发测试正文",
                embedding_id="per_d_v13_00001",
            )
        )
        resource = Resource(
            document_id=doc.id,
            course_id=public_course.id,
            uploader_id=uploader.id,
            title="并发投稿",
            review_status="co_reviewing",
        )
        timeout_resource = Resource(
            document_id=timeout_doc.id,
            course_id=public_course.id,
            uploader_id=uploader.id,
            title="超时投稿",
            review_status="co_reviewing",
        )
        session.add_all([resource, timeout_resource])
        await session.flush()
        task = ReviewTask(
            resource_id=resource.id,
            stage="co_review",
            assignee_ids=[reviewer.id, reviewer2.id],
        )
        timeout_task = ReviewTask(
            resource_id=timeout_resource.id,
            stage="co_review",
            assignee_ids=[reviewer.id, reviewer2.id],
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        post = Post(
            author_id=uploader.id,
            board="qa",
            course_id=public_course.id,
            title="并发重试",
            content="正文",
        )
        session.add_all([task, timeout_task, post])
        await session.commit()
        return {
            "admin": admin.id,
            "uploader": uploader.id,
            "reviewer": reviewer.id,
            "reviewer3": reviewer3.id,
            "resource": resource.id,
            "task": task.id,
            "course": public_course.id,
            "storage_key": doc.storage_key,
            "submission_doc": submission_doc.id,
            "timeout_resource": timeout_resource.id,
            "timeout_task": timeout_task.id,
            "post": post.id,
        }


async def test_real_pg_row_locks_and_unique_fallbacks(monkeypatch):
    ids = await _fixtures()
    monkeypatch.setattr(workflow, "get_embedding", lambda: _Embedding())
    monkeypatch.setattr(workflow, "get_vector_store", lambda: _VectorStore())

    async def _invalidate(*_args):
        return None

    monkeypatch.setattr(workflow, "invalidate_bm25", _invalidate)

    async def _co_verdict():
        async with SessionLocal() as session:
            reviewer = await session.get(User, ids["reviewer"])
            return await submit_verdict(
                ids["task"], VerdictRequest(verdict="approve"), reviewer, session
            )

    co_results = await asyncio.gather(_co_verdict(), _co_verdict(), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in co_results) == 1
    assert sum(isinstance(result, HTTPException) and result.status_code == 422 for result in co_results) == 1

    async with SessionLocal() as session:
        task = await session.get(ReviewTask, ids["task"])
        task.stage = "final"
        await session.commit()

    async def _side_effects(*_args):
        return {"preview": False, "chapter_summaries": False}

    monkeypatch.setattr("app.api.admin._approval_side_effects", _side_effects)

    async def _final_verdict():
        async with SessionLocal() as session:
            admin = await session.get(User, ids["admin"])
            return await final_review(
                FinalVerdictRequest(task_id=ids["task"], verdict="approve"), admin, session
            )

    final_results = await asyncio.gather(
        _final_verdict(), _final_verdict(), return_exceptions=True
    )
    assert sum(isinstance(result, dict) for result in final_results) == 1
    assert sum(
        isinstance(result, HTTPException) and result.status_code == 404
        for result in final_results
    ) == 1

    async with SessionLocal() as session:
        public_count = await session.scalar(
            select(func.count(Document.id)).where(
                Document.storage_key == ids["storage_key"], Document.scope == "public"
            )
        )
        record_count = await session.scalar(
            select(func.count(ReviewRecord.id)).where(
                ReviewRecord.task_id == ids["task"], ReviewRecord.stage == "final"
            )
        )
        score_count = await session.scalar(
            select(func.count(ScoreLog.id)).where(
                ScoreLog.reason == "upload_approved",
                ScoreLog.ref_id == ids["resource"],
            )
        )
        uploader = await session.get(User, ids["uploader"])
        assert (public_count, record_count, score_count, uploader.score) == (1, 1, 1, 20)

    async def _favorite():
        async with SessionLocal() as session:
            user = await session.get(User, ids["reviewer"])
            response = Response()
            result = await add_favorite(
                FavoriteRequest(target_type="resource", target_id=ids["resource"]),
                response,
                user,
                session,
            )
            return response.status_code, result

    favorite_results = await asyncio.gather(_favorite(), _favorite())
    assert sorted(result["created"] for _, result in favorite_results) == [False, True]
    async with SessionLocal() as session:
        favorite_count = await session.scalar(select(func.count(Favorite.id)))
        resource = await session.get(Resource, ids["resource"])
        assert favorite_count == resource.fav_count == 1

    async def _follow():
        async with SessionLocal() as session:
            user = await session.get(User, ids["reviewer"])
            response = Response()
            result = await add_follow(
                FollowRequest(target_type="course", target_id=ids["course"]),
                response,
                user,
                session,
            )
            return response.status_code, result

    follow_results = await asyncio.gather(_follow(), _follow())
    assert sorted(result["created"] for _, result in follow_results) == [False, True]
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count(Follow.id))) == 1

    async def _submit():
        async with SessionLocal() as session:
            doc = await session.get(Document, ids["submission_doc"])
            course = await session.get(Course, ids["course"])
            uploader = await session.get(User, ids["uploader"])
            result = await create_submission(
                session, uploader, doc, course, None, "并发新投稿", None
            )
            await session.commit()
            return result

    submission_results = await asyncio.gather(_submit(), _submit(), return_exceptions=True)
    assert sum(isinstance(result, tuple) for result in submission_results) == 1
    assert sum(
        isinstance(result, ValueError) and str(result) == "duplicate"
        for result in submission_results
    ) == 1
    winning = next(result for result in submission_results if isinstance(result, tuple))
    submission_task_id = winning[1].id
    async with SessionLocal() as session:
        resource_count = await session.scalar(
            select(func.count(Resource.id)).where(Resource.document_id == ids["submission_doc"])
        )
        task_count = await session.scalar(
            select(func.count(ReviewTask.id))
            .join(Resource, Resource.id == ReviewTask.resource_id)
            .where(Resource.document_id == ids["submission_doc"])
        )
        assert (resource_count, task_count) == (1, 1)

    monkeypatch.setattr(
        review_worker, "run_precheck", async_value({"hard_fail": False, "reason": ""})
    )
    await asyncio.gather(
        precheck_submission({}, submission_task_id),
        precheck_submission({}, submission_task_id),
    )
    async with SessionLocal() as session:
        submission_task = await session.get(ReviewTask, submission_task_id)
        assignment_notifications = await session.scalar(
            select(func.count(Notification.id)).where(Notification.type == "review_assign")
        )
        assert submission_task.stage == "co_review"
        assert assignment_notifications == 2

    redis = FakeRedis()
    pool = FakeArqPool()
    monkeypatch.setattr(posts_api, "get_redis", lambda: redis)
    monkeypatch.setattr(posts_api, "get_arq_pool", async_value(pool))

    async def _retry_ai():
        async with SessionLocal() as session:
            uploader = await session.get(User, ids["uploader"])
            return await retry_post_ai(
                ids["post"], AiRetryRequest(kind="answer"), uploader, session
            )

    retry_results = await asyncio.gather(_retry_ai(), _retry_ai())
    assert sorted(result["queued"] for result in retry_results) == [False, True]
    assert pool.jobs == [("generate_ai_first_answer", ids["post"])]

    monkeypatch.setattr(review_timeout_worker, "get_config", async_value(48))
    monkeypatch.setattr(
        review_timeout_worker, "assign_reviewers", async_value([ids["reviewer3"]])
    )
    timeout_results = await asyncio.gather(review_timeout_scan({}), review_timeout_scan({}))
    assert sum(result["reassigned"] for result in timeout_results) == 1
    async with SessionLocal() as session:
        timeout_task = await session.get(ReviewTask, ids["timeout_task"])
        timeout_notifications = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.type == "review_assign",
                Notification.user_id == ids["reviewer3"],
                Notification.title == "新协审任务：超时投稿",
            )
        )
        assert timeout_task.assignee_ids == [ids["reviewer3"]]
        assert timeout_notifications == 1
