"""v0.9 API 路由覆盖测试（§18.1）：posts / documents / resources / admin 主流程与关键分支。

与 test_growth.py 同一约定：CI 无 DB/Redis/Chroma/LLM——dependency_overrides 注入
FakeSession 与内存用户，ARQ 入队 / 对象存储 / 向量库 / grant_score / 平台配置全部
monkeypatch 为内存替身（见 tests/fakestack.py）。execute 结果按调用顺序预置出队。
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api import admin as admin_api
from app.api import documents as documents_api
from app.api import posts as posts_api
from app.api import resources as resources_api
from app.community.posts import ai_answer_key, exp_summary_key
from app.storage.models import (
    AuditLog,
    Chapter,
    Chunk,
    Comment,
    Course,
    Document,
    Major,
    Notification,
    Post,
    Report,
    Resource,
    ResourceRating,
    ReviewTask,
    User,
)

from .fakestack import (
    NOW,
    FakeArqPool,
    FakeEmbedding,
    FakeObjectStore,
    FakeRedis,
    FakeResult,
    FakeSession,
    FakeVectorStore,
    GrantRecorder,
    async_value,
    http_client,
    make_chunk,
    make_comment,
    make_course,
    make_doc,
    make_major,
    make_post,
    make_resource,
    make_task,
    make_user,
)

USER = make_user(1)
ADMIN = make_user(1, role="admin")


def _patch_posts_common(monkeypatch, redis=None, pool=None):
    """posts 路由公共边界：平台配置 / Redis / ARQ 池。"""
    redis = redis or FakeRedis()
    pool = pool or FakeArqPool()
    monkeypatch.setattr(posts_api, "get_config", async_value(60))
    monkeypatch.setattr(posts_api, "get_redis", lambda: redis)
    monkeypatch.setattr(posts_api, "get_arq_pool", async_value(pool))
    return redis, pool


# ---------------------------------------------------------------------------
# POST /api/posts：发帖
# ---------------------------------------------------------------------------


def test_create_post_invalid_board_and_bounty_rules():
    session = FakeSession()
    with http_client(session, USER) as client:
        resp = client.post("/api/posts/", json={"board": "bad", "title": "t", "content": "c"})
        assert resp.status_code == 422
        # 非求援板块带悬赏分：不静默忽略，直接 422
        resp = client.post(
            "/api/posts/",
            json={"board": "discuss", "title": "t", "content": "c", "bounty_score": 5},
        )
        assert resp.status_code == 422
        assert "仅资料求援板块支持悬赏" in resp.json()["detail"]
        # 求援板块悬赏分超出 1~1000 区间
        resp = client.post(
            "/api/posts/",
            json={"board": "bounty", "title": "t", "content": "c", "bounty_score": 0},
        )
        assert resp.status_code == 422


def test_create_post_qa_success_enqueues_ai_answer(monkeypatch):
    redis, pool = _patch_posts_common(monkeypatch)
    session = FakeSession(gets={(Course, 5): make_course()})
    with http_client(session, USER) as client:
        resp = client.post(
            "/api/posts/",
            json={"board": "qa", "title": "死锁条件？", "content": "请问", "course_id": 5},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["ai_first_answer_pending"] is True
    assert pool.jobs == [("generate_ai_first_answer", body["post_id"])]
    assert redis.kv[ai_answer_key(body["post_id"])] == "pending"
    post = next(o for o in session.added if isinstance(o, Post))
    assert post.board == "qa" and post.course_id == 5 and post.major_id == 3


def test_create_post_qa_requires_course_and_valid_scope(monkeypatch):
    _patch_posts_common(monkeypatch)
    with http_client(FakeSession(), USER) as client:
        resp = client.post("/api/posts/", json={"board": "qa", "title": "t", "content": "c"})
        assert resp.status_code == 422
        assert "必须指定课程" in resp.json()["detail"]
    # 课程不存在 / 非公共 / 未开放
    for gets in (
        {},
        {(Course, 5): make_course(scope="personal", owner_id=1)},
        {(Course, 5): make_course(status="pending")},
    ):
        with http_client(FakeSession(gets=gets), USER) as client:
            resp = client.post(
                "/api/posts/",
                json={"board": "qa", "title": "t", "content": "c", "course_id": 5},
            )
            assert resp.status_code == 422
            assert "课程不存在或未开放" in resp.json()["detail"]


def test_create_post_chapter_rules(monkeypatch):
    _patch_posts_common(monkeypatch)
    course = make_course()
    # 指定章节但未指定课程
    with http_client(FakeSession(), USER) as client:
        resp = client.post(
            "/api/posts/",
            json={"board": "discuss", "title": "t", "content": "c", "chapter_id": 8},
        )
        assert resp.status_code == 422
        assert "须同时指定课程" in resp.json()["detail"]
    # 章节不属于该课程
    gets = {(Course, 5): course, (Chapter, 8): SimpleNamespace(id=8, course_id=99)}
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post(
            "/api/posts/",
            json={"board": "qa", "title": "t", "content": "c", "course_id": 5, "chapter_id": 8},
        )
        assert resp.status_code == 422
        assert "章节不存在或不属于该课程" in resp.json()["detail"]


def test_create_post_experience_enqueues_summary(monkeypatch):
    redis, pool = _patch_posts_common(monkeypatch)
    session = FakeSession()
    with http_client(session, USER) as client:
        resp = client.post(
            "/api/posts/",
            json={"board": "experience", "title": "保研经验", "content": "正文",
                  "tags": ["保研", "保研", "夏令营"]},  # 标签去重
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["ai_summary_pending"] is True
    assert pool.jobs == [("generate_experience_summary", body["post_id"])]
    assert redis.kv[exp_summary_key(body["post_id"])] == "pending"
    post = next(o for o in session.added if isinstance(o, Post))
    assert post.tags == ["保研", "夏令营"]


def test_create_post_bounty_escrow_and_insufficient(monkeypatch):
    redis, pool = _patch_posts_common(monkeypatch)
    grants = GrantRecorder()
    monkeypatch.setattr(posts_api, "grant_score", grants)
    # 托管成功：行锁加载本人，扣 bounty_escrow
    session = FakeSession(gets={(User, 1): make_user(1, score=100)})
    with http_client(session, USER) as client:
        resp = client.post(
            "/api/posts/",
            json={"board": "bounty", "title": "求资料", "content": "c", "bounty_score": 10},
        )
    assert resp.status_code == 201
    assert pool.jobs == []  # 求援帖无 AI 任务
    assert grants.calls[0]["delta"] == -10
    assert grants.calls[0]["reason"] == "bounty_escrow"
    post = next(o for o in session.added if isinstance(o, Post))
    assert post.bounty_score == 10
    # 贡献分不足：422，不建档
    session = FakeSession(gets={(User, 1): make_user(1, score=5)})
    with http_client(session, USER) as client:
        resp = client.post(
            "/api/posts/",
            json={"board": "bounty", "title": "求资料", "content": "c", "bounty_score": 10},
        )
    assert resp.status_code == 422
    assert "贡献分不足" in resp.json()["detail"]


def test_create_post_muted_user_403_and_lazy_unmute(monkeypatch):
    _patch_posts_common(monkeypatch)
    now = datetime.now(UTC)
    muted = make_user(1, status="muted", muted_until=now + timedelta(days=1))
    with http_client(FakeSession(), muted) as client:
        resp = client.post("/api/posts/", json={"board": "discuss", "title": "t", "content": "c"})
        assert resp.status_code == 403
    # 禁言已到期：惰性解除后放行
    expired = make_user(1, status="muted", muted_until=now - timedelta(days=1))
    with http_client(FakeSession(), expired) as client:
        resp = client.post("/api/posts/", json={"board": "discuss", "title": "t", "content": "c"})
        assert resp.status_code == 201
    assert expired.status == "active" and expired.muted_until is None


def test_create_post_enqueue_failure_compensates(monkeypatch):
    redis, _ = _patch_posts_common(monkeypatch, pool=FakeArqPool(fail=True))
    session = FakeSession(gets={(Course, 5): make_course()})
    with http_client(session, USER) as client:
        resp = client.post(
            "/api/posts/",
            json={"board": "qa", "title": "t", "content": "c", "course_id": 5},
        )
    assert resp.status_code == 500
    assert "入队失败" in resp.json()["detail"]
    # 补偿：pending 键已删除，不残留轮询状态
    assert not any(k.startswith("knowisle:ai_answer:") for k in redis.kv)


# ---------------------------------------------------------------------------
# GET /api/posts：列表 / 详情 / 评论
# ---------------------------------------------------------------------------


def test_list_posts_with_aggregates_and_summary_status(monkeypatch):
    redis, _ = _patch_posts_common(monkeypatch)
    redis.kv[exp_summary_key(102)] = "pending"
    post_qa = make_post(101, board="qa")
    post_exp = make_post(102, board="experience", ai_summary=None)
    author = make_user(9, nickname="作者")
    session = FakeSession(results=[
        FakeResult(scalar=2),                                   # total
        FakeResult(rows=[(post_qa, author), (post_exp, author)]),  # 本页帖子
        FakeResult(rows=[(101, 3)]),                            # 评论数聚合
        FakeResult(rows=[(101, 5)]),                            # 投票分聚合
    ])
    with http_client(session, USER) as client:
        resp = client.get("/api/posts/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    qa_item, exp_item = body["items"]
    assert qa_item["comment_count"] == 3 and qa_item["vote_score"] == 5
    assert qa_item["has_ai_answer"] is False
    assert exp_item["ai_summary_status"] == "pending"  # mget 批量取状态键
    assert exp_item["comment_count"] == 0


def test_list_posts_empty_and_filters(monkeypatch):
    _patch_posts_common(monkeypatch)
    session = FakeSession(results=[FakeResult(scalar=0), FakeResult(rows=[])])
    with http_client(session, USER) as client:
        resp = client.get("/api/posts/", params={"board": "qa", "course_id": 5, "tag": "考研"})
    assert resp.status_code == 200
    assert resp.json() == {"total": 0, "items": []}


def test_get_post_404_and_anonymous_qa_pending(monkeypatch):
    redis, _ = _patch_posts_common(monkeypatch)
    with http_client(FakeSession(), None) as client:
        assert client.get("/api/posts/7").status_code == 404
    # 匿名访问：view_count+1，跳过 my_vote 查询；无答案时读 Redis 状态
    redis.kv[ai_answer_key(7)] = "pending"
    post = make_post(7, board="qa", ai_first_answer=None)
    session = FakeSession(
        gets={(Post, 7): post, (User, 1): make_user(1)},
        results=[FakeResult(scalar=7)],
    )
    with http_client(session, None) as client:
        resp = client.get("/api/posts/7")
    assert resp.status_code == 200
    body = resp.json()
    assert post.view_count == 11
    assert body["vote_score"] == 7 and body["my_vote"] == 0
    assert body["ai_answer_status"] == "pending"
    assert body["ai_citations"] == []


def test_get_post_logged_in_with_ai_answer_and_citations(monkeypatch):
    _patch_posts_common(monkeypatch)
    citations = [{"chunk_id": "pub_d1_00001", "document_id": 3, "chapter": None,
                  "section": None, "page_num": None, "uploader": "用户9"}]
    monkeypatch.setattr(posts_api, "build_ai_citations", async_value(citations))
    post = make_post(7, board="qa", ai_first_answer="根据 [pub_d1_00001] 可知")
    session = FakeSession(
        gets={(Post, 7): post, (User, 1): make_user(1)},
        results=[FakeResult(scalar=4), FakeResult(scalar=-1)],  # 总分 / 我的投票
    )
    with http_client(session, USER) as client:
        resp = client.get("/api/posts/7")
    body = resp.json()
    assert body["ai_answer_status"] == "done"
    assert body["my_vote"] == -1
    assert body["ai_citations"][0]["chunk_id"] == "pub_d1_00001"


def test_list_comments_scores_and_my_votes():
    post = make_post(7)
    comment = make_comment(31, post_id=7, author_id=9)
    author = make_user(9)
    gets = {(Post, 7): post}
    with http_client(FakeSession(), None) as client:
        assert client.get("/api/posts/99/comments").status_code == 404
    # 匿名：无 my_vote 查询
    session = FakeSession(gets=gets, results=[
        FakeResult(rows=[(comment, author)]),
        FakeResult(rows=[(31, 4)]),
    ])
    with http_client(session, None) as client:
        body = client.get("/api/posts/7/comments").json()
    assert body["items"][0]["vote_score"] == 4 and body["items"][0]["my_vote"] == 0
    # 登录：附我的投票
    session = FakeSession(gets=gets, results=[
        FakeResult(rows=[(comment, author)]),
        FakeResult(rows=[(31, 4)]),
        FakeResult(rows=[(31, -1)]),
    ])
    with http_client(session, USER) as client:
        body = client.get("/api/posts/7/comments").json()
    assert body["items"][0]["my_vote"] == -1
    assert body["items"][0]["author"]["nickname"] == "用户9"


def test_create_comment_rules_and_bounty_notification(monkeypatch):
    _patch_posts_common(monkeypatch)
    # 帖子不存在 / 已关闭 / 父评论不属于本帖
    with http_client(FakeSession(), USER) as client:
        assert client.post("/api/posts/7/comments", json={"content": "c"}).status_code == 404
    gets = {(Post, 7): make_post(7, status="closed")}
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post("/api/posts/7/comments", json={"content": "c"})
        assert resp.status_code == 422 and "已关闭" in resp.json()["detail"]
    gets = {
        (Post, 7): make_post(7),
        (Comment, 50): make_comment(50, post_id=99),  # 父评论属别的帖
    }
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post("/api/posts/7/comments", json={"content": "c", "parent_id": 50})
        assert resp.status_code == 422 and "父评论" in resp.json()["detail"]
    # 求援帖被响应：通知帖主（楼中楼不重复通知）
    bounty_post = make_post(7, board="bounty", author_id=9)
    session = FakeSession(gets={(Post, 7): bounty_post})
    with http_client(session, USER) as client:
        resp = client.post("/api/posts/7/comments", json={"content": "我有这份资料"})
    assert resp.status_code == 201
    notifications = [o for o in session.added if isinstance(o, Notification)]
    assert len(notifications) == 1
    assert notifications[0].type == "bounty" and notifications[0].user_id == 9
    parent = make_comment(50, post_id=7)
    session = FakeSession(gets={(Post, 7): bounty_post, (Comment, 50): parent})
    with http_client(session, USER) as client:
        resp = client.post("/api/posts/7/comments", json={"content": "追问", "parent_id": 50})
    assert resp.status_code == 201
    assert not [o for o in session.added if isinstance(o, Notification)]


# ---------------------------------------------------------------------------
# POST /api/posts/comments/{id}/accept：采纳与悬赏结算
# ---------------------------------------------------------------------------


def _accept_setup(monkeypatch):
    grants = GrantRecorder()
    monkeypatch.setattr(posts_api, "grant_score", grants)
    return grants


def test_accept_comment_404_and_not_owner(monkeypatch):
    _accept_setup(monkeypatch)
    with http_client(FakeSession(), USER) as client:
        assert client.post("/api/posts/comments/31/accept").status_code == 404
    # 越权（非帖主）：统一 404，不暴露存在性
    gets = {(Comment, 31): make_comment(31), (Post, 1): make_post(1, author_id=9)}
    with http_client(FakeSession(gets=gets), USER) as client:
        assert client.post("/api/posts/comments/31/accept").status_code == 404


def test_accept_comment_board_and_bounty_guards(monkeypatch):
    _accept_setup(monkeypatch)
    comment = make_comment(31)
    # 讨论帖不可采纳
    gets = {(Comment, 31): comment, (Post, 1): make_post(1, board="discuss")}
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post("/api/posts/comments/31/accept")
        assert resp.status_code == 422 and "仅问答贴或求援贴" in resp.json()["detail"]
    # 求援帖不能采纳自己的响应
    gets = {
        (Comment, 31): make_comment(31, author_id=1),
        (Post, 1): make_post(1, board="bounty", author_id=1),
    }
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post("/api/posts/comments/31/accept")
        assert resp.status_code == 422 and "不能采纳自己的响应" in resp.json()["detail"]
    # 已结算不可改采
    gets = {
        (Comment, 31): comment,
        (Post, 1): make_post(1, board="bounty", author_id=1, accepted_comment_id=32),
    }
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post("/api/posts/comments/31/accept")
        assert resp.status_code == 422 and "不可改采" in resp.json()["detail"]


def test_accept_comment_qa_first_accept_grants_score(monkeypatch):
    grants = _accept_setup(monkeypatch)
    comment = make_comment(31, author_id=9)
    post = make_post(1, board="qa", author_id=1, course_id=5)
    session = FakeSession(gets={(Comment, 31): comment, (Post, 1): post})
    with http_client(session, USER) as client:
        resp = client.post("/api/posts/comments/31/accept")
    assert resp.status_code == 200
    assert resp.json() == {"accepted": True, "score_granted": 15}
    assert comment.is_accepted and post.accepted_comment_id == 31
    assert grants.calls[0]["delta"] == 15 and grants.calls[0]["reason"] == "answer_accepted"
    assert grants.calls[0]["course_id"] == 5
    notifications = [o for o in session.added if isinstance(o, Notification)]
    assert notifications[0].type == "accepted" and notifications[0].user_id == 9


def test_accept_comment_qa_self_answer_no_score(monkeypatch):
    grants = _accept_setup(monkeypatch)
    comment = make_comment(31, author_id=1)  # 自问自答
    post = make_post(1, board="qa", author_id=1)
    session = FakeSession(gets={(Comment, 31): comment, (Post, 1): post})
    with http_client(session, USER) as client:
        resp = client.post("/api/posts/comments/31/accept")
    assert resp.json()["score_granted"] == 0
    assert grants.calls == []
    assert comment.is_accepted


def test_accept_comment_idempotent_and_reaccept(monkeypatch):
    grants = _accept_setup(monkeypatch)
    # 重复采纳同一条：幂等，不再计分
    comment = make_comment(31, author_id=9, is_accepted=True)
    post = make_post(1, board="qa", author_id=1, accepted_comment_id=31)
    with http_client(FakeSession(gets={(Comment, 31): comment, (Post, 1): post}), USER) as client:
        resp = client.post("/api/posts/comments/31/accept")
    assert resp.json() == {"accepted": True, "score_granted": 0}
    # 改采另一条：旧评论取消标记，积分不动
    old = make_comment(32, author_id=8, is_accepted=True)
    new = make_comment(31, author_id=9)
    post = make_post(1, board="qa", author_id=1, accepted_comment_id=32)
    gets = {(Comment, 31): new, (Comment, 32): old, (Post, 1): post}
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post("/api/posts/comments/31/accept")
    assert resp.json()["score_granted"] == 0
    assert old.is_accepted is False
    assert new.is_accepted and post.accepted_comment_id == 31
    assert grants.calls == []


def test_accept_comment_bounty_settles_escrow(monkeypatch):
    grants = _accept_setup(monkeypatch)
    comment = make_comment(31, author_id=9)
    post = make_post(1, board="bounty", author_id=1, bounty_score=20)
    session = FakeSession(gets={(Comment, 31): comment, (Post, 1): post})
    with http_client(session, USER) as client:
        resp = client.post("/api/posts/comments/31/accept")
    assert resp.json()["score_granted"] == 20
    # 托管赏金全额转给响应者，双方均收通知
    assert grants.calls[0]["user_id"] == 9
    assert grants.calls[0]["delta"] == 20 and grants.calls[0]["reason"] == "bounty_award"
    notifications = [o for o in session.added if isinstance(o, Notification)]
    assert {n.user_id for n in notifications} == {1, 9}


# ---------------------------------------------------------------------------
# /api/documents：上传 / 状态轮询 / 切块 / 投稿
# ---------------------------------------------------------------------------


def _upload(client, name="笔记.pdf", data=b"pdf-bytes", course_id="5"):
    return client.post(
        "/api/documents/",
        files={"file": (name, data, "application/pdf")},
        data={"course_id": course_id},
    )


def _patch_documents(monkeypatch, pool=None):
    pool = pool or FakeArqPool()
    monkeypatch.setattr(documents_api, "get_config", async_value(60))
    monkeypatch.setattr(documents_api, "get_arq_pool", async_value(pool))
    return pool


def test_upload_document_course_ownership(monkeypatch):
    _patch_documents(monkeypatch)
    for gets in (
        {},  # 课程不存在
        {(Course, 5): make_course()},  # 公共课程不能上传到个人库
        {(Course, 5): make_course(scope="personal", owner_id=9)},  # 他人个人课程
    ):
        with http_client(FakeSession(gets=gets), USER) as client:
            assert _upload(client).status_code == 404


def test_upload_document_file_validation(monkeypatch):
    _patch_documents(monkeypatch)
    gets = {(Course, 5): make_course(scope="personal", owner_id=1)}
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = _upload(client, name="旧版.doc")
        assert resp.status_code == 422 and "旧版 Office" in resp.json()["detail"]
        resp = _upload(client, name="程序.exe")
        assert resp.status_code == 422 and "不支持的文件类型" in resp.json()["detail"]
        resp = _upload(client, name="空.pdf", data=b"")
        assert resp.status_code == 422 and "空文件" in resp.json()["detail"]


def test_upload_document_success_enqueues_parse(monkeypatch):
    pool = _patch_documents(monkeypatch)
    store = FakeObjectStore()
    monkeypatch.setattr(documents_api, "object_store", store)
    gets = {(Course, 5): make_course(scope="personal", owner_id=1)}
    session = FakeSession(gets=gets, results=[FakeResult(rows=[])])  # 无同 md5 文档
    with http_client(session, USER) as client:
        resp = _upload(client, name="课件.pptx")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "parsing"
    assert pool.jobs == [("parse_document", body["document_id"])]
    # 存储键包含归属与文件名；新档为个人库 ppt 文档
    key, content_type = store.put_calls[0]
    assert key.startswith("personal/1/") and key.endswith("课件.pptx")
    assert content_type == "application/pdf"
    doc = next(o for o in session.added if isinstance(o, Document))
    assert doc.file_type == "ppt" and doc.scope == "personal"


def test_upload_document_dedup_and_failed_retry(monkeypatch):
    pool = _patch_documents(monkeypatch)
    store = FakeObjectStore()
    monkeypatch.setattr(documents_api, "object_store", store)
    gets = {(Course, 5): make_course(scope="personal", owner_id=1)}
    # 同 md5 已解析：幂等返回，不重复占用存储、不入队
    existing = make_doc(11, owner_id=1, status="parsed")
    session = FakeSession(gets=gets, results=[FakeResult(rows=[existing])])
    with http_client(session, USER) as client:
        resp = _upload(client)
    body = resp.json()
    assert body == {"document_id": 11, "status": "parsed", "dedup": True}
    assert pool.jobs == [] and store.put_calls == []
    # 同 md5 解析失败：重置状态重新入队
    failed = make_doc(12, owner_id=1, status="failed")
    session = FakeSession(gets=gets, results=[FakeResult(rows=[failed])])
    with http_client(session, USER) as client:
        resp = _upload(client)
    body = resp.json()
    assert body["retry"] is True and body["dedup"] is True
    assert failed.status == "parsing"
    assert pool.jobs == [("parse_document", 12)]


def test_document_status_and_chunks_permission():
    doc = make_doc(11, owner_id=1)
    # 状态轮询：本人可见，附切块数；他人 404
    session = FakeSession(gets={(Document, 11): doc}, results=[FakeResult(scalar=4)])
    with http_client(session, USER) as client:
        body = client.get("/api/documents/11/status").json()
    assert body == {"document_id": 11, "file_name": "笔记.pdf", "status": "parsed", "chunk_count": 4}
    with http_client(FakeSession(gets={(Document, 11): doc}), make_user(2)) as client:
        assert client.get("/api/documents/11/status").status_code == 404
    # 切块：个人文档他人 404；admin 可见；public 文档任何登录用户可见
    chunk = make_chunk(document_id=11)
    with http_client(FakeSession(gets={(Document, 11): doc}), make_user(2)) as client:
        assert client.get("/api/documents/11/chunks").status_code == 404
    session = FakeSession(gets={(Document, 11): doc},
                          results=[FakeResult(rows=[(chunk, "第 1 章")])])
    with http_client(session, make_user(2, role="admin")) as client:
        body = client.get("/api/documents/11/chunks").json()
    assert body["items"][0]["chapter_title"] == "第 1 章"
    public_doc = make_doc(12, owner_id=9, scope="public")
    session = FakeSession(gets={(Document, 12): public_doc},
                          results=[FakeResult(rows=[(chunk, None)])])
    with http_client(session, make_user(2)) as client:
        assert client.get("/api/documents/12/chunks").status_code == 200


def test_submit_to_public_guards(monkeypatch):
    _patch_documents(monkeypatch)
    doc = make_doc(11, owner_id=1, status="parsed")
    # 他人文档：admin 也不可代投
    with http_client(FakeSession(), USER) as client:
        resp = client.post("/api/documents/11/submit",
                           json={"title": "t", "course_id": 5})
        assert resp.status_code == 404
    # 仅个人库可投 / 解析完成才能投
    for bad_doc in (make_doc(11, owner_id=1, scope="public"),
                    make_doc(11, owner_id=1, status="parsing")):
        with http_client(FakeSession(gets={(Document, 11): bad_doc}), USER) as client:
            resp = client.post("/api/documents/11/submit",
                               json={"title": "t", "course_id": 5})
            assert resp.status_code == 422
    # 目标课程不存在 / 章节不属于目标课程
    gets = {(Document, 11): doc}
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post("/api/documents/11/submit", json={"title": "t", "course_id": 5})
        assert resp.status_code == 404
    gets = {
        (Document, 11): doc,
        (Course, 5): make_course(),
        (Chapter, 8): SimpleNamespace(id=8, course_id=99),
    }
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post("/api/documents/11/submit",
                           json={"title": "t", "course_id": 5, "chapter_id": 8})
        assert resp.status_code == 422 and "章节" in resp.json()["detail"]


def test_submit_to_public_success_and_conflicts(monkeypatch):
    pool = _patch_documents(monkeypatch)
    doc = make_doc(11, owner_id=1, status="parsed")
    gets = {(Document, 11): doc, (Course, 5): make_course()}
    # 成功：建档入队预检；重投复用原 resource（resubmit=True）
    resource, task = make_resource(review_status="pending"), make_task(stage="precheck")
    monkeypatch.setattr(documents_api, "create_submission",
                        async_value((resource, task, True)))
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post("/api/documents/11/submit", json={"title": "笔记", "course_id": 5})
    assert resp.status_code == 201
    body = resp.json()
    assert body == {"resource_id": 21, "review_status": "pending",
                    "review_task_id": 41, "resubmit": True}
    assert pool.jobs == [("precheck_submission", 41)]
    # 审核中 / 已上架冲突
    for error, detail in (("duplicate", "该资料已在审核中"), ("approved", "该资料已上架")):
        async def _raise(*_a, _e=error):
            raise ValueError(_e)
        monkeypatch.setattr(documents_api, "create_submission", _raise)
        with http_client(FakeSession(gets=gets), USER) as client:
            resp = client.post("/api/documents/11/submit",
                               json={"title": "笔记", "course_id": 5})
            assert resp.status_code == 422
            assert resp.json()["detail"] == detail


# ---------------------------------------------------------------------------
# /api/resources：检索 / 详情 / 预览 / 下载 / 评分 / 克隆
# ---------------------------------------------------------------------------


def _patch_resources(monkeypatch, redis=None, store=None):
    redis = redis or FakeRedis()
    store = store or FakeObjectStore()
    grants = GrantRecorder()
    monkeypatch.setattr(resources_api, "get_redis", lambda: redis)
    monkeypatch.setattr(resources_api, "object_store", store)
    monkeypatch.setattr(resources_api, "grant_score", grants)
    return redis, store, grants


def test_list_resources_search_and_filters():
    resource = make_resource()
    session = FakeSession(results=[
        FakeResult(scalar=1),
        FakeResult(rows=[(resource, "pdf_textbook", "用户9")]),
    ])
    with http_client(session, USER) as client:
        resp = client.get("/api/resources/",
                          params={"course_id": 5, "chapter_id": 8, "q": "笔记%_"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["title"] == "操作系统笔记"
    assert item["uploader"] == {"id": 9, "nickname": "用户9"}
    assert item["file_type"] == "pdf_textbook"


def test_get_resource_visibility_and_preview_flag():
    # 审核中的资源：他人 404；本人可见
    pending = make_resource(review_status="pending")
    with http_client(FakeSession(gets={(Resource, 21): pending}), USER) as client:
        assert client.get("/api/resources/21").status_code == 404
    doc = make_doc(11, owner_id=9)
    uploader = make_user(9)
    gets = {(Resource, 21): pending, (Document, 11): doc, (User, 9): uploader}
    session = FakeSession(gets=gets, results=[FakeResult(scalar=None)])
    with http_client(session, make_user(9)) as client:
        body = client.get("/api/resources/21").json()
    assert body["preview_available"] is True  # pdf 原件可直接预览
    assert body["my_rating"] is None
    # word 资源：预览可用性看终审派生 public 副本的 preview_key
    word_doc = make_doc(11, owner_id=9, file_type="word")
    public_copy = make_doc(12, scope="public", preview_key="preview/12/m.pdf")
    gets = {
        (Resource, 21): make_resource(),
        (Document, 11): word_doc,
        (User, 9): uploader,
    }
    session = FakeSession(gets=gets, results=[
        FakeResult(scalar=4),                       # my_rating
        FakeResult(rows=[public_copy]),             # public 副本
    ])
    with http_client(session, USER) as client:
        body = client.get("/api/resources/21").json()
    assert body["preview_available"] is True and body["my_rating"] == 4


def test_preview_resource_permission_and_streams(monkeypatch):
    _patch_resources(monkeypatch, store=FakeObjectStore({"k/11": "# 标题".encode()}))
    md_doc = make_doc(11, owner_id=9, file_type="markdown")
    # 审核中 + 他人 + 非协审指派：404；被指派协审员放行
    pending = make_resource(review_status="pending")
    gets = {(Resource, 21): pending, (Document, 11): md_doc}
    session = FakeSession(gets=gets, results=[FakeResult(rows=[])])
    with http_client(session, USER) as client:
        assert client.get("/api/resources/21/preview").status_code == 404
    session = FakeSession(gets=gets, results=[FakeResult(rows=[(41,)])])
    with http_client(session, USER) as client:
        resp = client.get("/api/resources/21/preview")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.headers["Content-Disposition"] == "inline"
    assert resp.content == "# 标题".encode()
    # 已上架 pdf：任何登录用户直接流式预览
    pdf_doc = make_doc(11, owner_id=9, file_type="pdf_textbook")
    gets = {(Resource, 21): make_resource(), (Document, 11): pdf_doc}
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.get("/api/resources/21/preview")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"


def test_preview_resource_office_transitional_states(monkeypatch):
    _patch_resources(monkeypatch, store=FakeObjectStore({"preview/12/m.pdf": b"pdf"}))
    word_doc = make_doc(11, owner_id=9, file_type="word")
    gets = {(Resource, 21): make_resource(), (Document, 11): word_doc}
    # 转换产物未就绪：409 预览生成中
    public_copy = make_doc(12, scope="public", preview_key=None)
    session = FakeSession(gets=gets, results=[FakeResult(rows=[public_copy])])
    with http_client(session, USER) as client:
        resp = client.get("/api/resources/21/preview")
        assert resp.status_code == 409 and "预览生成中" in resp.json()["detail"]
    # 产物就绪：流式返回 PDF
    public_copy.preview_key = "preview/12/m.pdf"
    session = FakeSession(gets=gets, results=[FakeResult(rows=[public_copy])])
    with http_client(session, USER) as client:
        resp = client.get("/api/resources/21/preview")
        assert resp.status_code == 200 and resp.content == b"pdf"


def test_download_resource_not_approved_and_paid(monkeypatch):
    _, store, grants = _patch_resources(monkeypatch,
                                        store=FakeObjectStore({"k/11": b"data"}))
    # 未上架：本人/admin 也不可下载
    gets = {(Resource, 21): make_resource(review_status="pending", uploader_id=1)}
    with http_client(FakeSession(gets=gets), USER) as client:
        assert client.get("/api/resources/21/download").status_code == 404
    # 付费下载：扣分 + 80% 分成给上传者
    resource = make_resource(download_cost=10, uploader_id=9)
    doc = make_doc(11, owner_id=9)
    gets = {(Resource, 21): resource, (Document, 11): doc, (User, 1): make_user(1, score=100)}
    session = FakeSession(gets=gets)
    with http_client(session, USER) as client:
        resp = client.get("/api/resources/21/download")
    assert resp.status_code == 200 and resp.content == b"data"
    assert resource.download_count == 4
    assert [(c["user_id"], c["delta"], c["reason"]) for c in grants.calls] == [
        (1, -10, "download_cost"),
        (9, 8, "download_share"),
    ]
    # 贡献分不足：422，不计分
    gets[(User, 1)] = make_user(1, score=5)
    grants.calls.clear()
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.get("/api/resources/21/download")
        assert resp.status_code == 422 and "贡献分不足" in resp.json()["detail"]
    assert grants.calls == []


def test_download_resource_free_reward_and_redis_failure(monkeypatch):
    redis = FakeRedis()
    _, store, grants = _patch_resources(monkeypatch, redis=redis,
                                        store=FakeObjectStore({"k/11": b"data"}))
    resource = make_resource(download_cost=0, uploader_id=9)
    doc = make_doc(11, owner_id=9)
    gets = {(Resource, 21): resource, (Document, 11): doc}
    # 免费下载：首次下载给上传者 +1（Redis 去重 + 每日上限计数）
    with http_client(FakeSession(gets=gets), USER) as client:
        assert client.get("/api/resources/21/download").status_code == 200
    assert "1" in redis.sets["knowisle:dled:21"]
    assert grants.calls[0]["reason"] == "download_reward"
    assert grants.calls[0]["delta"] == 1
    # 同一用户重复下载：不再奖励
    grants.calls.clear()
    with http_client(FakeSession(gets=gets), USER) as client:
        assert client.get("/api/resources/21/download").status_code == 200
    assert grants.calls == []
    # 上传者下载自己的资源：无奖励也不扣分
    with http_client(FakeSession(gets=gets), make_user(9)) as client:
        assert client.get("/api/resources/21/download").status_code == 200
    assert grants.calls == []


def test_download_resource_free_reward_tolerates_redis_down(monkeypatch):
    _, store, grants = _patch_resources(monkeypatch,
                                        store=FakeObjectStore({"k/11": b"data"}))

    def _broken():
        raise ConnectionError("redis down")

    monkeypatch.setattr(resources_api, "get_redis", _broken)
    resource = make_resource(download_cost=0, uploader_id=9)
    gets = {(Resource, 21): resource, (Document, 11): make_doc(11, owner_id=9)}
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.get("/api/resources/21/download")
    assert resp.status_code == 200  # Redis 故障跳过奖励不阻塞下载
    assert grants.calls == []


def test_rate_resource_rules_and_upsert():
    # 未上架 404；不能给自己的资源评分
    gets = {(Resource, 21): make_resource(review_status="pending")}
    with http_client(FakeSession(gets=gets), USER) as client:
        assert client.post("/api/resources/21/rating", json={"stars": 5}).status_code == 404
    gets = {(Resource, 21): make_resource(uploader_id=1)}
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post("/api/resources/21/rating", json={"stars": 5})
        assert resp.status_code == 422 and "不能给自己的资源评分" in resp.json()["detail"]
        assert client.post("/api/resources/21/rating", json={"stars": 0}).status_code == 422
    # 首次评分：新增 ResourceRating 并回写平均分
    resource = make_resource()
    session = FakeSession(gets={(Resource, 21): resource}, results=[
        FakeResult(scalar=None),        # 无既有评分
        FakeResult(rows=[(4.0, 1)]),    # avg / count
    ])
    with http_client(session, USER) as client:
        body = client.post("/api/resources/21/rating", json={"stars": 4}).json()
    assert body == {"rating": 4.0, "rating_count": 1, "my_rating": 4}
    assert any(isinstance(o, ResourceRating) for o in session.added)
    assert resource.rating == 4.0 and resource.rating_count == 1
    # 重复评分：覆盖原记录
    existing = SimpleNamespace(id=1, user_id=1, resource_id=21, stars=4)
    resource = make_resource(rating=4.0, rating_count=1)
    session = FakeSession(gets={(Resource, 21): resource}, results=[
        FakeResult(scalar=existing),
        FakeResult(rows=[(3.0, 1)]),
    ])
    with http_client(session, USER) as client:
        body = client.post("/api/resources/21/rating", json={"stars": 2}).json()
    assert existing.stars == 2
    assert body["rating"] == 3.0 and body["my_rating"] == 2


def test_clone_resource_guards():
    # 未上架 404；目标课程不存在/非本人个人课程 404
    with http_client(FakeSession(), USER) as client:
        assert client.post("/api/resources/21/clone").status_code == 404
    src_doc = make_doc(11, owner_id=9, scope="public")
    for gets in (
        {(Resource, 21): make_resource(), (Document, 11): src_doc},
        {(Resource, 21): make_resource(), (Document, 11): src_doc,
         (Course, 7): make_course(7, scope="personal", owner_id=9)},
    ):
        with http_client(FakeSession(gets=gets), USER) as client:
            resp = client.post("/api/resources/21/clone", json={"course_id": 7})
            assert resp.status_code == 404


def test_clone_resource_idempotent_hit():
    src_doc = make_doc(11, owner_id=9, scope="public")
    existing = make_doc(55, owner_id=1, course_id=7)
    gets = {(Resource, 21): make_resource(), (Document, 11): src_doc}
    session = FakeSession(gets=gets, results=[FakeResult(rows=[existing])])
    with http_client(session, USER) as client:
        body = client.post("/api/resources/21/clone").json()
    assert body == {"document_id": 55, "course_id": 7, "cloned": False}


def test_clone_resource_success_with_explicit_course(monkeypatch):
    _, _, _ = _patch_resources(monkeypatch)
    embedding, vector_store = FakeEmbedding(), FakeVectorStore()
    invalidated = []
    monkeypatch.setattr(resources_api, "get_embedding", lambda: embedding)
    monkeypatch.setattr(resources_api, "get_vector_store", lambda: vector_store)
    monkeypatch.setattr(resources_api, "invalidate_bm25",
                        lambda *args: invalidated.append(args))
    src_doc = make_doc(11, owner_id=9, scope="public", preview_key="preview/11/m.pdf")
    target = make_course(7, scope="personal", owner_id=1)
    gets = {(Resource, 21): make_resource(), (Document, 11): src_doc, (Course, 7): target}
    chunks = [make_chunk("pub_d11_00001", 11), make_chunk("pub_d11_00002", 11, id=2)]
    session = FakeSession(gets=gets, results=[
        FakeResult(rows=[]),       # 幂等检查：无同 storage_key 文档
        FakeResult(rows=chunks),   # 源 chunks
    ])
    with http_client(session, USER) as client:
        resp = client.post("/api/resources/21/clone", json={"course_id": 7})
    assert resp.status_code == 201
    body = resp.json()
    assert body["cloned"] is True and body["course_id"] == 7
    new_doc = next(o for o in session.added if isinstance(o, Document))
    assert new_doc.scope == "personal" and new_doc.preview_key == "preview/11/m.pdf"
    new_chunks = [o for o in session.added if isinstance(o, Chunk)]
    assert [c.chunk_id for c in new_chunks] == [
        f"per_d{body['document_id']}_00001", f"per_d{body['document_id']}_00002",
    ]
    assert new_chunks[0].chapter_id is None  # 公共章节对个人课程无意义
    assert vector_store.upserts[0][0] == "chunks_personal"
    assert len(embedding.calls) == 1
    assert invalidated == [(7, "personal", 1)]


def test_clone_resource_default_course_and_preview_fallback(monkeypatch):
    _, _, _ = _patch_resources(monkeypatch)
    monkeypatch.setattr(resources_api, "get_embedding", FakeEmbedding)
    monkeypatch.setattr(resources_api, "get_vector_store", FakeVectorStore)
    monkeypatch.setattr(resources_api, "invalidate_bm25", lambda *a: None)
    # 原件无 preview_key：回退取终审派生 public 副本的预览键
    src_doc = make_doc(11, owner_id=9, scope="public", preview_key=None)
    public_copy = make_doc(12, scope="public", preview_key="preview/12/m.pdf")
    gets = {(Resource, 21): make_resource(), (Document, 11): src_doc}
    session = FakeSession(gets=gets, results=[
        FakeResult(rows=[]),               # 幂等检查
        FakeResult(rows=[]),               # 无既有「公共库克隆」课程
        FakeResult(rows=[public_copy]),    # public 副本预览键
        FakeResult(rows=[make_chunk()]),   # 源 chunks
    ])
    with http_client(session, USER) as client:
        resp = client.post("/api/resources/21/clone")
    assert resp.status_code == 201
    body = resp.json()
    course = next(o for o in session.added if isinstance(o, Course))
    assert course.name == "公共库克隆" and course.semester_id is None
    assert body["course_id"] == course.id
    new_doc = next(o for o in session.added if isinstance(o, Document))
    assert new_doc.preview_key == "preview/12/m.pdf"
    assert new_doc.course_id == course.id


def test_clone_resource_empty_chunks_skips_embedding(monkeypatch):
    _, _, _ = _patch_resources(monkeypatch)
    embedding = FakeEmbedding()
    monkeypatch.setattr(resources_api, "get_embedding", lambda: embedding)
    monkeypatch.setattr(resources_api, "get_vector_store", FakeVectorStore)
    invalidated = []
    monkeypatch.setattr(resources_api, "invalidate_bm25",
                        lambda *args: invalidated.append(args))
    src_doc = make_doc(11, owner_id=9, scope="public", preview_key="pk")
    target = make_course(7, scope="personal", owner_id=1)
    gets = {(Resource, 21): make_resource(), (Document, 11): src_doc, (Course, 7): target}
    session = FakeSession(gets=gets, results=[
        FakeResult(rows=[]),  # 幂等检查
        FakeResult(rows=[]),  # 源 chunks 为空
    ])
    with http_client(session, USER) as client:
        resp = client.post("/api/resources/21/clone", json={"course_id": 7})
    assert resp.status_code == 201
    assert embedding.calls == []      # 无 chunks 不向量化
    assert invalidated == []          # 也不失效 BM25 缓存


# ---------------------------------------------------------------------------
# /api/admin：空间管理 / 终审 / 改派 / 直审 / 用户治理 / 配置 / 精华 / 举报 / 看板
# ---------------------------------------------------------------------------


def test_admin_endpoints_role_gate():
    with http_client(FakeSession(), None) as client:
        assert client.get("/api/admin/users").status_code == 401
    with http_client(FakeSession(), USER) as client:
        assert client.get("/api/admin/users").status_code == 404  # 非 admin 统一 404


def test_import_majors_idempotent():
    session = FakeSession(results=[FakeResult(rows=["数学"])])  # 已存在专业
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/majors", json={
            "items": [{"name": "数学"}, {"name": "计算机", "code": "CS"}],
        })
    assert resp.status_code == 201
    assert resp.json() == {"created": 1, "skipped": 1, "names": ["计算机"]}
    majors = [o for o in session.added if isinstance(o, Major)]
    assert len(majors) == 1 and majors[0].name == "计算机" and majors[0].code == "CS"
    audit = next(o for o in session.added if isinstance(o, AuditLog))
    assert audit.action == "import_majors"
    assert audit.detail == {"created": 1, "skipped": 1}


def test_update_course_status():
    with http_client(FakeSession(), ADMIN) as client:
        assert client.patch("/api/admin/courses/5", json={"status": "active"}).status_code == 404
    with http_client(FakeSession(gets={(Course, 5): make_course(scope="personal", owner_id=1)}),
                     ADMIN) as client:
        assert client.patch("/api/admin/courses/5", json={"status": "active"}).status_code == 404
    course = make_course(status="pending")
    session = FakeSession(gets={(Course, 5): course})
    with http_client(session, ADMIN) as client:
        resp = client.patch("/api/admin/courses/5",
                            json={"status": "active", "description": "已审批"})
    assert resp.status_code == 200
    assert resp.json() == {"id": 5, "name": "操作系统", "status": "active"}
    assert course.description == "已审批"
    audit = next(o for o in session.added if isinstance(o, AuditLog))
    assert audit.action == "course_status"
    assert audit.detail == {"course_id": 5, "from": "pending", "to": "active"}


def test_list_review_tasks_default_and_by_stage():
    task_co = make_task(41, stage="co_review")
    task_done = make_task(42, stage="done", finished_at=NOW)
    resource = make_resource()
    rows_co = [(task_co, resource, "操作系统", "用户9")]
    rows_done = [(task_done, resource, "操作系统", "用户9")]
    # 默认：协审/终审中 + 最近完成 20 条，附意见数
    session = FakeSession(results=[
        FakeResult(rows=rows_co),
        FakeResult(rows=rows_done),
        FakeResult(rows=[(41, 2)]),
    ])
    with http_client(session, ADMIN) as client:
        body = client.get("/api/admin/review/tasks").json()
    assert len(body["items"]) == 2
    item = body["items"][0]
    assert item["task_id"] == 41 and item["record_count"] == 2
    assert item["resource"]["course_name"] == "操作系统"
    assert body["items"][1]["record_count"] == 0
    # 指定 stage：单查询
    session = FakeSession(results=[FakeResult(rows=rows_co), FakeResult(rows=[(41, 2)])])
    with http_client(session, ADMIN) as client:
        body = client.get("/api/admin/review/tasks", params={"stage": "co_review"}).json()
    assert len(body["items"]) == 1


def test_final_review_verdict_flows(monkeypatch):
    task = make_task(41, stage="final")
    with http_client(FakeSession(), ADMIN) as client:
        resp = client.post("/api/admin/review/final", json={"task_id": 41, "verdict": "approve"})
        assert resp.status_code == 404
    # 驳回必须填写理由；其他领域错误统一 404
    for error, status in (("comment_required", 422), ("task_done", 404)):
        async def _raise(*_a, _e=error):
            raise ValueError(_e)
        monkeypatch.setattr(admin_api, "final_verdict", _raise)
        with http_client(FakeSession(gets={(ReviewTask, 41): task}), ADMIN) as client:
            resp = client.post("/api/admin/review/final",
                               json={"task_id": 41, "verdict": "reject"})
            assert resp.status_code == status
    # 通过：派生 public 副本为 word → 入队预览转换 + 摘要回填
    pool = FakeArqPool()
    monkeypatch.setattr(admin_api, "final_verdict", async_value((20, 55)))
    monkeypatch.setattr(admin_api, "get_arq_pool", async_value(pool))
    gets = {
        (ReviewTask, 41): task,
        (Resource, 21): make_resource(),
        (Document, 55): make_doc(55, file_type="word"),
    }
    session = FakeSession(gets=gets)
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/review/final",
                           json={"task_id": 41, "verdict": "approve"})
    assert resp.status_code == 200
    assert resp.json() == {"resource_id": 21, "review_status": "approved", "score_granted": 20}
    assert pool.jobs == [("convert_preview", 55), ("backfill_chapter_summaries", 5)]
    audit = next(o for o in session.added if isinstance(o, AuditLog))
    assert audit.action == "final_verdict"
    # 通过但非 word/ppt：只入队摘要回填
    pool.jobs.clear()
    gets[(Document, 55)] = make_doc(55, file_type="pdf_textbook")
    with http_client(FakeSession(gets=gets), ADMIN) as client:
        client.post("/api/admin/review/final", json={"task_id": 41, "verdict": "approve"})
    assert pool.jobs == [("backfill_chapter_summaries", 5)]
    # 驳回：不入队任何任务
    pool.jobs.clear()
    monkeypatch.setattr(admin_api, "final_verdict", async_value((0, None)))
    with http_client(FakeSession(gets={(ReviewTask, 41): task}), ADMIN) as client:
        resp = client.post("/api/admin/review/final",
                           json={"task_id": 41, "verdict": "reject", "comment": "质量不足"})
    assert resp.json()["review_status"] == "rejected"
    assert pool.jobs == []


def _reassign_gets(task=None):
    task = task or make_task(41, stage="co_review", assignee_ids=[3, 4])
    return task, {(ReviewTask, 41): task, (Resource, 21): make_resource()}


def test_reassign_guards():
    with http_client(FakeSession(), ADMIN) as client:
        assert client.post("/api/admin/review/reassign", json={"task_id": 41}).status_code == 404
    task = make_task(41, stage="final")
    with http_client(FakeSession(gets={(ReviewTask, 41): task}), ADMIN) as client:
        resp = client.post("/api/admin/review/reassign", json={"task_id": 41})
        assert resp.status_code == 422 and "不在协审阶段" in resp.json()["detail"]


def test_reassign_explicit_reviewer_validation():
    task, gets = _reassign_gets()
    reviewer = make_user(7, role="reviewer")
    # 指定不存在的协审员
    session = FakeSession(gets=gets, results=[FakeResult(rows=[]), FakeResult(rows=[])])
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/review/reassign",
                           json={"task_id": 41, "reviewer_ids": [9]})
        assert resp.status_code == 422 and "协审员不合格" in resp.json()["detail"]
    # 协审员已提交意见：不可重复指派
    session = FakeSession(gets=gets, results=[FakeResult(rows=[7]), FakeResult(rows=[reviewer])])
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/review/reassign",
                           json={"task_id": 41, "reviewer_ids": [7]})
        assert resp.status_code == 422 and "已提交意见" in resp.json()["detail"]


def test_reassign_explicit_success_notifies():
    task, gets = _reassign_gets()
    reviewers = [make_user(7, role="reviewer"), make_user(8, role="reviewer")]
    session = FakeSession(gets=gets, results=[FakeResult(rows=[]), FakeResult(rows=reviewers)])
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/review/reassign",
                           json={"task_id": 41, "reviewer_ids": [7, 8, 7]})  # 去重
    body = resp.json()
    assert body == {"task_id": 41, "assignee_ids": [7, 8], "escalated": False}
    assert task.assignee_ids == [7, 8]
    notifications = [o for o in session.added if isinstance(o, Notification)]
    assert {n.user_id for n in notifications} == {7, 8}
    assert all(n.type == "review_assign" for n in notifications)


def test_reassign_auto_merge_and_escalate(monkeypatch):
    # 自动补足：已提交意见者保留，排除原指派者后补新人
    task, gets = _reassign_gets()
    monkeypatch.setattr(admin_api, "assign_reviewers", async_value([11]))
    session = FakeSession(gets=gets, results=[FakeResult(rows=[3])])  # 3 已提交
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/review/reassign", json={"task_id": 41})
    assert resp.json() == {"task_id": 41, "assignee_ids": [3, 11], "escalated": False}
    # 无可用协审员且无已提交意见：兜底直送终审并通知全体在职管理员
    task, gets = _reassign_gets()
    monkeypatch.setattr(admin_api, "assign_reviewers", async_value([]))
    admin2 = make_user(50, role="admin")
    session = FakeSession(gets=gets, results=[
        FakeResult(rows=[]),
        FakeResult(rows=[admin2]),
    ])
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/review/reassign", json={"task_id": 41})
    assert resp.json() == {"task_id": 41, "assignee_ids": [], "escalated": True}
    assert task.stage == "final"
    notifications = [o for o in session.added if isinstance(o, Notification)]
    assert notifications[0].type == "review_escalate" and notifications[0].user_id == 50


def test_direct_review_flows(monkeypatch):
    task = make_task(41, stage="co_review")
    with http_client(FakeSession(), ADMIN) as client:
        resp = client.post("/api/admin/review/direct", json={"task_id": 41, "verdict": "approve"})
        assert resp.status_code == 404
    for error, detail in (("comment_required", "驳回必须填写理由"),
                          ("precheck", "预检未完成，无法直审"),
                          ("done", "任务已完结"),
                          ("other", None)):
        async def _raise(*_a, _e=error):
            raise ValueError(_e)
        monkeypatch.setattr(admin_api, "direct_verdict", _raise)
        with http_client(FakeSession(gets={(ReviewTask, 41): task}), ADMIN) as client:
            resp = client.post("/api/admin/review/direct",
                               json={"task_id": 41, "verdict": "approve"})
            assert resp.status_code == (404 if detail is None else 422)
            if detail is not None:
                assert resp.json()["detail"] == detail
    # 直审通过：同样入队摘要回填（markdown 不转预览）
    pool = FakeArqPool()
    monkeypatch.setattr(admin_api, "direct_verdict", async_value((15, 77)))
    monkeypatch.setattr(admin_api, "get_arq_pool", async_value(pool))
    gets = {
        (ReviewTask, 41): task,
        (Resource, 21): make_resource(),
        (Document, 77): make_doc(77, file_type="markdown"),
    }
    session = FakeSession(gets=gets)
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/review/direct",
                           json={"task_id": 41, "verdict": "approve"})
    assert resp.json() == {"resource_id": 21, "review_status": "approved", "score_granted": 15}
    assert pool.jobs == [("backfill_chapter_summaries", 5)]
    audit = next(o for o in session.added if isinstance(o, AuditLog))
    assert audit.action == "review_direct_verdict"
    assert audit.detail["from_stage"] == "co_review"


def test_list_users_search_pagination_and_privacy():
    user = make_user(9, nickname="小李", major_id=3)
    session = FakeSession(results=[
        FakeResult(scalar=1),
        FakeResult(rows=[user]),
        FakeResult(rows=[make_major()]),
    ])
    with http_client(session, ADMIN) as client:
        body = client.get("/api/admin/users", params={"q": "小李%"}).json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["nickname"] == "小李" and item["major_name"] == "计算机"
    assert "real_name" not in item  # 真实姓名不下发（§3.1 隐私）


def test_update_user_role_rules():
    with http_client(FakeSession(), ADMIN) as client:
        resp = client.put("/api/admin/users/9/role", json={"role": "reviewer"})
        assert resp.status_code == 404
    # 不能修改自己的角色（防自降权锁死）
    gets = {(User, 1): make_user(1, role="admin")}
    with http_client(FakeSession(gets=gets), ADMIN) as client:
        resp = client.put("/api/admin/users/1/role", json={"role": "student"})
        assert resp.status_code == 422
    # 角色未变化：幂等
    target = make_user(9, role="student")
    with http_client(FakeSession(gets={(User, 9): target}), ADMIN) as client:
        resp = client.put("/api/admin/users/9/role", json={"role": "student"})
        assert resp.json() == {"id": 9, "role": "student", "changed": False}
    # 任命 reviewer：通知本人 + 审计
    session = FakeSession(gets={(User, 9): target})
    with http_client(session, ADMIN) as client:
        resp = client.put("/api/admin/users/9/role", json={"role": "reviewer"})
    assert resp.json() == {"id": 9, "role": "reviewer", "changed": True}
    assert target.role == "reviewer"
    notification = next(o for o in session.added if isinstance(o, Notification))
    assert notification.type == "role_change" and notification.user_id == 9


def test_adjust_credit_rules(monkeypatch):
    with http_client(FakeSession(), ADMIN) as client:
        resp = client.post("/api/admin/users/9/credit", json={"delta": 0, "reason": "r"})
        assert resp.status_code == 422
        resp = client.post("/api/admin/users/9/credit", json={"delta": -15, "reason": "违规"})
        assert resp.status_code == 404
    monkeypatch.setattr(admin_api, "apply_credit_change", async_value(85))
    target = make_user(9)
    session = FakeSession(gets={(User, 9): target})
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/users/9/credit",
                           json={"delta": -15, "reason": "违规", "ref_type": "post", "ref_id": 1})
    assert resp.json() == {"id": 9, "credit": 85, "delta": -15}
    audit = next(o for o in session.added if isinstance(o, AuditLog))
    assert audit.action == "credit_penalty" and audit.detail["credit"] == 85


def test_list_credit_logs():
    with http_client(FakeSession(), ADMIN) as client:
        assert client.get("/api/admin/users/9/credit-logs").status_code == 404
    log = SimpleNamespace(delta=-15, reason="违规", ref_type="post", ref_id=1, created_at=NOW)
    session = FakeSession(gets={(User, 9): make_user(9)},
                          results=[FakeResult(rows=[(log, "管理员")])])
    with http_client(session, ADMIN) as client:
        body = client.get("/api/admin/users/9/credit-logs").json()
    item = body["items"][0]
    assert item["delta"] == -15 and item["admin_nickname"] == "管理员"


def test_list_admin_courses():
    course = make_course()
    session = FakeSession(results=[
        FakeResult(scalar=1),
        FakeResult(rows=[(course, "计算机")]),
    ])
    with http_client(session, ADMIN) as client:
        body = client.get("/api/admin/courses", params={"status": "active", "major_id": 3}).json()
    assert body["total"] == 1
    assert body["items"][0]["major_name"] == "计算机"


def test_list_platform_config_overrides():
    row = SimpleNamespace(key="ai_daily_limit", value=50, updated_by=9, updated_at=NOW)
    session = FakeSession(results=[
        FakeResult(rows=[row]),
        FakeResult(rows=[make_user(9, nickname="运营")]),
    ])
    with http_client(session, ADMIN) as client:
        body = client.get("/api/admin/config").json()
    items = {i["key"]: i for i in body["items"]}
    overridden = items["ai_daily_limit"]
    assert overridden["value"] == 50 and overridden["overridden"] is True
    assert overridden["updated_by"] == "运营"
    default_only = items["credit_mute_days"]
    assert default_only["overridden"] is False
    assert default_only["value"] == default_only["default"]


def test_update_platform_config(monkeypatch):
    for error, status in (("unknown_key", 404), ("out_of_range: 0~10000", 422)):
        async def _raise(*_a, _e=error):
            raise ValueError(_e)
        monkeypatch.setattr(admin_api, "set_config", _raise)
        with http_client(FakeSession(), ADMIN) as client:
            resp = client.put("/api/admin/config/ai_daily_limit", json={"value": 50})
            assert resp.status_code == status
    monkeypatch.setattr(admin_api, "set_config", async_value((None, 30)))
    session = FakeSession()
    with http_client(session, ADMIN) as client:
        resp = client.put("/api/admin/config/ai_daily_limit", json={"value": 30})
    assert resp.json() == {"key": "ai_daily_limit", "value": 30, "previous": None}
    audit = next(o for o in session.added if isinstance(o, AuditLog))
    assert audit.action == "config_change"
    assert audit.detail == {"key": "ai_daily_limit", "from": None, "to": 30}


def test_feature_post_guards():
    with http_client(FakeSession(), ADMIN) as client:
        resp = client.post("/api/admin/posts/7/feature", json={"featured": True})
        assert resp.status_code == 404
    gets = {(Post, 7): make_post(7, board="qa")}
    with http_client(FakeSession(gets=gets), ADMIN) as client:
        resp = client.post("/api/admin/posts/7/feature", json={"featured": True})
        assert resp.status_code == 422 and "仅经验长廊" in resp.json()["detail"]
    gets = {(Post, 7): make_post(7, board="experience", author_id=1)}
    with http_client(FakeSession(gets=gets), ADMIN) as client:
        resp = client.post("/api/admin/posts/7/feature", json={"featured": True})
        assert resp.status_code == 422 and "不可标记自己的帖子" in resp.json()["detail"]


def test_feature_post_grant_once_and_unfeature(monkeypatch):
    grants = GrantRecorder()
    monkeypatch.setattr(admin_api, "grant_score", grants)
    # 首次标记：作者 +30 + 通知 + 审计
    post = make_post(7, board="experience", author_id=9, status="normal")
    session = FakeSession(gets={(Post, 7): post}, results=[FakeResult(scalar=0)])
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/posts/7/feature", json={"featured": True})
    assert resp.json() == {"post_id": 7, "featured": True, "score_granted": 30}
    assert post.status == "featured"
    assert grants.calls[0]["delta"] == 30 and grants.calls[0]["reason"] == "post_featured"
    notification = next(o for o in session.added if isinstance(o, Notification))
    assert notification.type == "featured" and notification.user_id == 9
    # 每帖仅发放一次：score_logs 留痕判定，取消后再标记不重复发放
    post2 = make_post(7, board="experience", author_id=9, status="normal")
    grants.calls.clear()
    session = FakeSession(gets={(Post, 7): post2}, results=[FakeResult(scalar=1)])
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/posts/7/feature", json={"featured": True})
    assert resp.json()["score_granted"] == 0 and grants.calls == []
    assert post2.status == "featured"
    # 已是精华：幂等
    post3 = make_post(7, board="experience", author_id=9, status="featured")
    with http_client(FakeSession(gets={(Post, 7): post3}), ADMIN) as client:
        resp = client.post("/api/admin/posts/7/feature", json={"featured": True})
    assert resp.json() == {"post_id": 7, "featured": True, "score_granted": 0}
    # 取消精华：只回退状态不动积分
    post4 = make_post(7, board="experience", author_id=9, status="featured")
    session = FakeSession(gets={(Post, 7): post4})
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/posts/7/feature", json={"featured": False})
    assert resp.json() == {"post_id": 7, "featured": False, "score_granted": 0}
    assert post4.status == "normal"
    # 取消非精华帖：无操作
    post5 = make_post(7, board="experience", author_id=9, status="normal")
    session = FakeSession(gets={(Post, 7): post5})
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/posts/7/feature", json={"featured": False})
    assert resp.json()["featured"] is False
    assert not [o for o in session.added if isinstance(o, AuditLog)]


def _report(report_id, target_type, target_id, status="open", handler_id=None):
    return SimpleNamespace(
        id=report_id, reporter_id=9, target_type=target_type, target_id=target_id,
        reason="不当内容", status=status, handler_id=handler_id, created_at=NOW,
    )


def test_list_reports_with_target_summaries():
    reports = [
        (_report(1, "resource", 21), "举报人"),
        (_report(2, "post", 7), "举报人"),
        (_report(3, "comment", 31), "举报人"),
        (_report(4, "user", 9, handler_id=50), "举报人"),
    ]
    session = FakeSession(results=[
        FakeResult(scalar=4),
        FakeResult(rows=reports),
        FakeResult(rows=[(21, "资源标题")]),
        FakeResult(rows=[(7, "帖子标题")]),
        FakeResult(rows=[(31, "评论内容")]),
        FakeResult(rows=[(9, "用户9")]),
        FakeResult(rows=[make_user(50, nickname="处理人")]),
    ])
    with http_client(session, ADMIN) as client:
        body = client.get("/api/admin/reports", params={"status": "open"}).json()
    assert body["total"] == 4
    by_type = {i["target_type"]: i for i in body["items"]}
    assert by_type["resource"]["target_summary"] == "资源标题"
    assert by_type["comment"]["target_summary"] == "评论内容"
    assert by_type["user"]["handler"] == {"id": 50, "nickname": "处理人"}
    assert by_type["post"]["handler"] is None


def test_handle_report_transitions():
    with http_client(FakeSession(), ADMIN) as client:
        resp = client.post("/api/admin/reports/1/handle", json={"action": "resolve"})
        assert resp.status_code == 404
    # 已结案不可再操作
    closed = _report(1, "post", 7, status="resolved")
    with http_client(FakeSession(gets={(Report, 1): closed}), ADMIN) as client:
        resp = client.post("/api/admin/reports/1/handle", json={"action": "resolve"})
        assert resp.status_code == 422 and "已结案" in resp.json()["detail"]
    # open → processing
    report = _report(1, "post", 7)
    session = FakeSession(gets={(Report, 1): report})
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/reports/1/handle", json={"action": "processing"})
    assert resp.json() == {"report_id": 1, "status": "processing"}
    assert report.handler_id == 1
    # 重复 processing：422
    with http_client(FakeSession(gets={(Report, 1): report}), ADMIN) as client:
        resp = client.post("/api/admin/reports/1/handle", json={"action": "processing"})
        assert resp.status_code == 422
    # resolve：通知举报人（含备注）
    session = FakeSession(gets={(Report, 1): report})
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/reports/1/handle",
                           json={"action": "resolve", "note": "已删除"})
    assert resp.json()["status"] == "resolved"
    notification = next(o for o in session.added if isinstance(o, Notification))
    assert notification.type == "report_result" and notification.user_id == 9
    assert "认定违规" in notification.body and "已删除" in notification.body
    # dismiss：通知结论为驳回
    report = _report(2, "user", 9)
    session = FakeSession(gets={(Report, 2): report})
    with http_client(session, ADMIN) as client:
        resp = client.post("/api/admin/reports/2/handle", json={"action": "dismiss"})
    assert resp.json()["status"] == "dismissed"
    notification = next(o for o in session.added if isinstance(o, Notification))
    assert "未认定违规" in notification.body


def test_admin_dashboard_aggregates():
    day = datetime(2026, 9, 29, tzinfo=UTC)
    session = FakeSession(results=[
        FakeResult(scalar=10),                    # users_total
        FakeResult(scalar=2),                     # new_today
        FakeResult(scalar=3),                     # active_today
        FakeResult(scalar=8),                     # resources_total
        FakeResult(scalar=1),                     # resources_pending
        FakeResult(scalar=20),                    # posts_total
        FakeResult(rows=[("qa", 10), ("discuss", 10)]),  # posts_by_board
        FakeResult(scalar=30),                    # comments_total
        FakeResult(scalar=2),                     # reports_open
        FakeResult(scalar=100),                   # qa_total
        FakeResult(scalar=5),                     # qa_today
        FakeResult(scalar=1234),                  # tokens_today
        FakeResult(scalar=250.4),                 # avg_latency
        FakeResult(rows=[(day, 3)]),              # 近 7 天 qa
        FakeResult(rows=[]),                      # 近 7 天 posts
        FakeResult(rows=[]),                      # 近 7 天 uploads
    ])
    with http_client(session, ADMIN) as client:
        body = client.get("/api/admin/dashboard").json()
    assert body["users"] == {"total": 10, "new_today": 2, "active_today": 3}
    assert body["content"]["posts_by_board"] == {"qa": 10, "discuss": 10}
    assert body["ai"]["avg_latency_ms_today"] == 250
    assert body["ai"]["tokens_today"] == 1234
    assert len(body["trend"]["days"]) == 7
    assert sum(body["trend"]["qa"]) == 3
    assert sum(body["trend"]["posts"]) == 0  # 缺日补 0


def test_admin_dashboard_avg_latency_none():
    # avg_latency 无数据（None）→ 0
    results = [FakeResult(scalar=0)] * 12 + [FakeResult(scalar=None)]
    results += [FakeResult(rows=[])] * 3
    session = FakeSession(results=results)
    with http_client(session, ADMIN) as client:
        body = client.get("/api/admin/dashboard").json()
    assert body["ai"]["avg_latency_ms_today"] == 0


# ---------------------------------------------------------------------------
# 补充分支：入队失败补偿 / 状态轮询 / 边界容错
# ---------------------------------------------------------------------------


def test_create_post_experience_enqueue_failure_compensates(monkeypatch):
    redis, _ = _patch_posts_common(monkeypatch, pool=FakeArqPool(fail=True))
    session = FakeSession()
    with http_client(session, USER) as client:
        resp = client.post(
            "/api/posts/",
            json={"board": "experience", "title": "t", "content": "c"},
        )
    assert resp.status_code == 500
    assert "AI 摘要任务入队失败" in resp.json()["detail"]
    assert not any(k.startswith("knowisle:exp_summary:") for k in redis.kv)


def test_get_post_experience_summary_pending(monkeypatch):
    redis, _ = _patch_posts_common(monkeypatch)
    redis.kv[exp_summary_key(7)] = "pending"
    post = make_post(7, board="experience", ai_summary=None)
    session = FakeSession(
        gets={(Post, 7): post, (User, 1): make_user(1)},
        results=[FakeResult(scalar=0)],
    )
    with http_client(session, None) as client:
        body = client.get("/api/posts/7").json()
    assert body["ai_summary_status"] == "pending"
    assert body["ai_answer_status"] == "none"  # 非 qa 帖恒为 none


def test_upload_document_oversize_rejected(monkeypatch):
    _patch_documents(monkeypatch)
    monkeypatch.setattr(documents_api, "MAX_FILE_SIZE", 8)  # 缩小限额免传 50MB
    gets = {(Course, 5): make_course(scope="personal", owner_id=1)}
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = _upload(client, data=b"x" * 16)
    assert resp.status_code == 422 and "50MB" in resp.json()["detail"]


def test_submit_to_public_unexpected_error_propagates(monkeypatch):
    _patch_documents(monkeypatch)
    doc = make_doc(11, owner_id=1, status="parsed")
    gets = {(Document, 11): doc, (Course, 5): make_course()}

    async def _raise(*_a):
        raise ValueError("db-deadlock")

    monkeypatch.setattr(documents_api, "create_submission", _raise)
    with http_client(FakeSession(gets=gets), USER) as client, pytest.raises(ValueError):
        client.post("/api/documents/11/submit", json={"title": "t", "course_id": 5})


def test_preview_resource_missing_returns_404(monkeypatch):
    _patch_resources(monkeypatch)
    with http_client(FakeSession(), USER) as client:
        assert client.get("/api/resources/21/preview").status_code == 404


def test_direct_review_approve_word_enqueues_preview(monkeypatch):
    pool = FakeArqPool()
    task = make_task(41, stage="co_review")
    monkeypatch.setattr(admin_api, "direct_verdict", async_value((15, 77)))
    monkeypatch.setattr(admin_api, "get_arq_pool", async_value(pool))
    gets = {
        (ReviewTask, 41): task,
        (Resource, 21): make_resource(),
        (Document, 77): make_doc(77, file_type="word"),
    }
    with http_client(FakeSession(gets=gets), ADMIN) as client:
        resp = client.post("/api/admin/review/direct",
                           json={"task_id": 41, "verdict": "approve"})
    assert resp.status_code == 200
    assert pool.jobs == [("convert_preview", 77), ("backfill_chapter_summaries", 5)]


# ---------------------------------------------------------------------------
# v0.9 缺陷修复用例：上传入队失败补偿 / 状态键 Redis 容错
# ---------------------------------------------------------------------------


def test_upload_document_enqueue_failure_marks_failed(monkeypatch):
    """解析任务入队失败：500 + 文档落回 failed 供重传，不永久卡 parsing。"""
    _patch_documents(monkeypatch, pool=FakeArqPool(fail=True))
    monkeypatch.setattr(documents_api, "object_store", FakeObjectStore())
    gets = {(Course, 5): make_course(scope="personal", owner_id=1)}
    session = FakeSession(gets=gets, results=[FakeResult(rows=[])])
    with http_client(session, USER) as client:
        resp = _upload(client)
    assert resp.status_code == 500
    assert "入队失败" in resp.json()["detail"]
    doc = next(o for o in session.added if isinstance(o, Document))
    assert doc.status == "failed"


def test_upload_document_retry_enqueue_failure_restores_failed(monkeypatch):
    """failed 重传分支：入队失败同样落回 failed（重试语义可由客户端再次触发）。"""
    _patch_documents(monkeypatch, pool=FakeArqPool(fail=True))
    monkeypatch.setattr(documents_api, "object_store", FakeObjectStore())
    gets = {(Course, 5): make_course(scope="personal", owner_id=1)}
    failed = make_doc(12, owner_id=1, status="failed")
    session = FakeSession(gets=gets, results=[FakeResult(rows=[failed])])
    with http_client(session, USER) as client:
        resp = _upload(client)
    assert resp.status_code == 500
    assert failed.status == "failed"


def test_get_post_tolerates_redis_failure(monkeypatch):
    """Redis 宕机：AI 首答/摘要状态键读取失败仅记日志，详情仍 200（按无状态键处理）。"""
    _patch_posts_common(monkeypatch)

    def _broken():
        raise ConnectionError("redis down")

    monkeypatch.setattr(posts_api, "get_redis", _broken)
    post = make_post(7, board="qa", ai_first_answer=None)
    session = FakeSession(
        gets={(Post, 7): post, (User, 1): make_user(1)},
        results=[FakeResult(scalar=0)],
    )
    with http_client(session, None) as client:
        resp = client.get("/api/posts/7")
    assert resp.status_code == 200
    assert resp.json()["ai_answer_status"] == "none"
    # 经验帖摘要状态键同理
    post = make_post(8, board="experience", ai_summary=None)
    session = FakeSession(
        gets={(Post, 8): post, (User, 1): make_user(1)},
        results=[FakeResult(scalar=0)],
    )
    with http_client(session, None) as client:
        resp = client.get("/api/posts/8")
    assert resp.status_code == 200
    assert resp.json()["ai_summary_status"] == "none"
