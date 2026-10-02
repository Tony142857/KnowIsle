"""v0.9 SSR 页面路由覆盖测试（§18.1）：pages.py 主要 GET 页面的权限分支与正常渲染。

目标不求全页面：覆盖未登录 303 / 越权 404 / 关键 200 渲染（真实 Jinja 模板 +
SimpleNamespace 数据）。DB 用 FakeSession 按序出队，Redis / 会话销毁 monkeypatch。
"""

from types import SimpleNamespace

from app.storage.models import Course, Document, Major, Post, Resource, User
from app.web.routes import pages

from .fakestack import (
    NOW,
    FakeRedis,
    FakeResult,
    FakeSession,
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


# ---------------------------------------------------------------------------
# 登录 / 登出 / 占位
# ---------------------------------------------------------------------------


def test_login_redirects_when_logged_in():
    with http_client(user=USER) as client:
        resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_logout_destroys_session_and_clears_cookie(monkeypatch):
    destroyed = []
    monkeypatch.setattr(pages, "get_redis", lambda: FakeRedis())

    async def _destroy(_redis, token):
        destroyed.append(token)

    monkeypatch.setattr(pages, "destroy_session", _destroy)
    with http_client() as client:
        resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert len(destroyed) == 1


def test_unknown_single_segment_is_404():
    with http_client() as client:
        resp = client.get("/no-such-page")
    assert resp.status_code == 404
    assert "text/html" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# 首页（v0.9 改版：Hero + 平台统计 + 动态流骨架）
# ---------------------------------------------------------------------------


def test_index_page_renders_stats_and_feed_skeleton():
    # 登录态：平台统计 SSR 注入 + 「我的动态 / 社区热帖」双栏骨架
    session = FakeSession(results=[
        FakeResult(scalar=12),  # 公共课程数
        FakeResult(scalar=34),  # 上架资源数
        FakeResult(scalar=56),  # 帖子数
    ])
    with http_client(session, USER) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "我的动态" in resp.text and "社区热帖" in resp.text
    assert "/api/feed" in resp.text
    assert "v0." not in resp.text  # 版本角标属开发痕迹，首页不再外露
    # 匿名态：无「我的动态」，渲染登录引导卡
    session = FakeSession(results=[
        FakeResult(scalar=1), FakeResult(scalar=2), FakeResult(scalar=3),
    ])
    with http_client(session) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert ">我的动态</h2>" not in resp.text  # 登录引导卡文案提及该词，故锚定面板标题
    assert "登录后解锁你的专属动态" in resp.text


def test_index_page_db_failure_degrades_stats():
    # DB 不可用：统计行降级为不展示，页面其余照常渲染（与板块列表降级惯例一致）
    with http_client(FakeSession(boom=True)) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "每门课程是一座知识岛屿" in resp.text


# ---------------------------------------------------------------------------
# 课程空间板块
# ---------------------------------------------------------------------------


def test_majors_pages():
    session = FakeSession(results=[FakeResult(rows=[(make_major(), 5)])])
    with http_client(session, None) as client:
        resp = client.get("/majors")
    assert resp.status_code == 200 and "计算机" in resp.text
    with http_client(FakeSession(), None) as client:
        assert client.get("/majors/3").status_code == 404
    session = FakeSession(
        gets={(Major, 3): make_major()},
        results=[FakeResult(rows=[make_course()])],
    )
    with http_client(session, None) as client:
        resp = client.get("/majors/3")
    assert resp.status_code == 200 and "操作系统" in resp.text


def test_course_detail_visibility():
    # 不存在 / 非公共课程 / 未开放且非 admin：404
    with http_client(FakeSession(), None) as client:
        assert client.get("/courses/5").status_code == 404
    gets = {(Course, 5): make_course(scope="personal", owner_id=1)}
    with http_client(FakeSession(gets=gets), USER) as client:
        assert client.get("/courses/5").status_code == 404
    gets = {(Course, 5): make_course(status="pending"), (Major, 3): make_major()}
    with http_client(FakeSession(gets=gets), None) as client:
        assert client.get("/courses/5").status_code == 404


def test_course_detail_renders_chapter_tree():
    ch1 = SimpleNamespace(id=1, course_id=5, title="第 1 章 概述", parent_id=None, order_idx=0)
    ch2 = SimpleNamespace(id=2, course_id=5, title="1.1 进程", parent_id=1, order_idx=1)
    gets = {(Course, 5): make_course(), (Major, 3): make_major()}
    # 匿名：跳过 is_following 查询
    session = FakeSession(gets=gets, results=[
        FakeResult(rows=[ch1, ch2]),
        FakeResult(scalar=2),
    ])
    with http_client(session, None) as client:
        resp = client.get("/courses/5")
    assert resp.status_code == 200
    assert "操作系统" in resp.text and "第 1 章 概述" in resp.text
    # admin 可见未开放课程（含我的关注状态查询）
    gets = {(Course, 5): make_course(status="pending"), (Major, 3): make_major()}
    session = FakeSession(gets=gets, results=[
        FakeResult(rows=[ch1]),
        FakeResult(scalar=0),
        FakeResult(scalar=None),
    ])
    with http_client(session, ADMIN) as client:
        assert client.get("/courses/5").status_code == 200


# ---------------------------------------------------------------------------
# 个人知识库板块
# ---------------------------------------------------------------------------


def test_library_page_requires_login_and_renders_tree():
    with http_client(FakeSession(), None) as client:
        resp = client.get("/library", follow_redirects=False)
        assert resp.status_code == 303
    semester = SimpleNamespace(id=1, owner_id=1, name="2025 秋")
    course = make_course(7, scope="personal", owner_id=1, semester_id=1, name="我的课程")
    chapter = SimpleNamespace(id=1, course_id=7, title="第 1 章", parent_id=None, order_idx=0)
    doc = make_doc(11, owner_id=1, course_id=7)
    resource = make_resource(21, document_id=11, review_status="pending")
    session = FakeSession(results=[
        FakeResult(rows=[semester]),
        FakeResult(rows=[course]),
        FakeResult(rows=[chapter]),
        FakeResult(rows=[doc]),
        FakeResult(rows=[resource]),
    ])
    with http_client(session, USER) as client:
        resp = client.get("/library")
    assert resp.status_code == 200
    assert "我的课程" in resp.text and "笔记.pdf" in resp.text
    # 空库：无章节/文档/投稿查询
    session = FakeSession(results=[FakeResult(rows=[]), FakeResult(rows=[])])
    with http_client(session, USER) as client:
        assert client.get("/library").status_code == 200


def test_library_chat_page():
    with http_client(FakeSession(), USER) as client:
        resp = client.get("/library/chat", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/library"
    gets = {(Course, 7): make_course(7, scope="personal", owner_id=9)}
    with http_client(FakeSession(gets=gets), USER) as client:
        assert client.get("/library/chat", params={"course_id": 7}).status_code == 404
    gets = {(Course, 7): make_course(7, scope="personal", owner_id=1)}
    with http_client(FakeSession(gets=gets), USER) as client:
        assert client.get("/library/chat", params={"course_id": 7}).status_code == 200


def test_document_source_page():
    doc = make_doc(11, owner_id=1)
    with http_client(FakeSession(gets={(Document, 11): doc}), None) as client:
        resp = client.get("/documents/11/source", follow_redirects=False)
        assert resp.status_code == 303
    with http_client(FakeSession(gets={(Document, 11): doc}), make_user(2)) as client:
        assert client.get("/documents/11/source").status_code == 404
    chunk = make_chunk(document_id=11)
    session = FakeSession(
        gets={(Document, 11): doc, (Course, 5): make_course(scope="personal", owner_id=1)},
        results=[FakeResult(rows=[chunk])],
    )
    with http_client(session, USER) as client:
        resp = client.get("/documents/11/source")
    assert resp.status_code == 200 and "进程与线程的区别" in resp.text


# ---------------------------------------------------------------------------
# 公共库板块：资源详情 / 协审工作台 / 管理员终审页
# ---------------------------------------------------------------------------


def test_resource_detail_page_visibility():
    pending = make_resource(review_status="pending")
    gets = {(Resource, 21): pending}
    with http_client(FakeSession(gets=gets), None) as client:
        resp = client.get("/resources/21", follow_redirects=False)
        assert resp.status_code == 303
    with http_client(FakeSession(gets=gets), USER) as client:
        assert client.get("/resources/21").status_code == 404


def test_resource_detail_page_renders():
    resource = make_resource()
    gets = {
        (Resource, 21): resource,
        (Document, 11): make_doc(11, owner_id=9),
        (User, 9): make_user(9, nickname="上传者"),
        (Course, 5): make_course(),
    }
    session = FakeSession(gets=gets, results=[
        FakeResult(scalar=4),     # my_rating
        FakeResult(scalar=None),  # is_favorited
    ])
    with http_client(session, USER) as client:
        resp = client.get("/resources/21")
    assert resp.status_code == 200
    assert "操作系统笔记" in resp.text and "上传者" in resp.text
    # word 资源：预览可用性走 public 副本查询
    gets[(Document, 11)] = make_doc(11, owner_id=9, file_type="word")
    public_copy = make_doc(12, scope="public", preview_key="preview/12/m.pdf")
    session = FakeSession(gets=gets, results=[
        FakeResult(scalar=None),
        FakeResult(rows=[public_copy]),
        FakeResult(scalar=1),  # 已收藏
    ])
    with http_client(session, USER) as client:
        assert client.get("/resources/21").status_code == 200


def _review_row():
    task = make_task(41, stage="co_review", assignee_ids=[8])
    return task, (task, make_resource(), "操作系统")


def test_review_workbench_role_gate():
    with http_client(FakeSession(), None) as client:
        resp = client.get("/review", follow_redirects=False)
        assert resp.status_code == 303
    with http_client(FakeSession(), USER) as client:
        assert client.get("/review").status_code == 404  # 非 reviewer/admin 404


def test_review_workbench_queue_and_selection():
    reviewer = make_user(8, role="reviewer")
    task, row = _review_row()
    # 任务队列
    session = FakeSession(results=[FakeResult(rows=[row])])
    with http_client(session, reviewer) as client:
        resp = client.get("/review")
    assert resp.status_code == 200 and "操作系统笔记" in resp.text
    # 选中本人被指派的任务：附预检结果与协审意见
    record = SimpleNamespace(id=1, task_id=41, reviewer_id=8, stage="co_review",
                             verdict="approve", comment="可以", created_at=NOW)
    session = FakeSession(
        gets={(Document, 11): make_doc(11, owner_id=9)},
        results=[
            FakeResult(rows=[row]),
            FakeResult(rows=[record]),
            FakeResult(rows=[reviewer]),
        ],
    )
    with http_client(session, reviewer) as client:
        resp = client.get("/review", params={"task": 41})
    assert resp.status_code == 200 and "可以" in resp.text
    # 选中未指派给本人的任务：404
    session = FakeSession(results=[FakeResult(rows=[row])])
    with http_client(session, reviewer) as client:
        assert client.get("/review", params={"task": 99}).status_code == 404


def test_admin_pages_role_gate():
    with http_client(FakeSession(), USER) as client:
        assert client.get("/admin").status_code == 404
    with http_client(FakeSession(), ADMIN) as client:
        assert client.get("/admin").status_code == 200


def test_admin_review_page_renders_queues():
    task = make_task(41, stage="final")
    record = SimpleNamespace(id=1, task_id=41, reviewer_id=8, stage="co_review",
                             verdict="approve", comment="同意", created_at=NOW)
    reviewer = make_user(8, role="reviewer")
    session = FakeSession(results=[
        FakeResult(rows=[(task, make_resource(), "操作系统", "用户9")]),  # final 队列
        FakeResult(rows=[record]),                                        # 意见批量查
        FakeResult(rows=[reviewer]),                                      # 协审员昵称
        FakeResult(rows=[]),                                              # co_review 队列
        FakeResult(rows=[]),                                              # done 队列
    ])
    with http_client(session, ADMIN) as client:
        resp = client.get("/admin/review")
    assert resp.status_code == 200 and "操作系统笔记" in resp.text


# ---------------------------------------------------------------------------
# 社区板块
# ---------------------------------------------------------------------------


def test_board_page_qa_renders_posts():
    post = make_post(101, board="qa")
    author = make_user(9, nickname="作者")
    session = FakeSession(results=[
        FakeResult(scalar=1),
        FakeResult(rows=[(post, author)]),
        FakeResult(rows=[(101, 2)]),
        FakeResult(rows=[(101, 3)]),
    ])
    with http_client(session, None) as client:
        resp = client.get("/boards/qa")
    assert resp.status_code == 200
    assert "测试帖子" in resp.text and "作者" in resp.text


def test_board_page_experience_tag_cloud_and_grades():
    post = make_post(101, board="experience", tags=["保研"], ai_summary="摘要")
    author = make_user(9, grade="2023级")
    session = FakeSession(results=[
        FakeResult(scalar=1),
        FakeResult(rows=[(post, author)]),
        FakeResult(rows=[]),
        FakeResult(rows=[]),
        FakeResult(rows=[["保研"], ["保研"], ["考研"]]),  # 标签频次
        FakeResult(rows=["2023级"]),                       # 届别去重
    ])
    with http_client(session, None) as client:
        resp = client.get("/boards/experience", params={"tag": "保研"})
    assert resp.status_code == 200 and "保研" in resp.text


def test_board_page_db_failure_degrades_to_skeleton():
    # DB 不可用：骨架照常渲染，列表由客户端补载
    with http_client(FakeSession(boom=True), None) as client:
        resp = client.get("/boards/discuss")
    assert resp.status_code == 200
    assert "讨论区" in resp.text


def test_post_new_page_board_fallback():
    with http_client(user=USER) as client:
        assert client.get("/posts/new").status_code == 200
        resp = client.get("/posts/new", params={"board": "experience"})
        assert resp.status_code == 200 and "经验长廊" in resp.text
        # 未知板块回落 qa
        resp = client.get("/posts/new", params={"board": "bad"})
        assert resp.status_code == 200 and "知屿问答" in resp.text


def test_post_detail_404_and_qa_pending(monkeypatch):
    with http_client(FakeSession(), None) as client:
        assert client.get("/posts/7").status_code == 404
    redis = FakeRedis()
    redis.kv[pages.ai_answer_key(7)] = "pending"
    monkeypatch.setattr(pages, "get_redis", lambda: redis)
    post = make_post(7, board="qa", ai_first_answer=None)
    comment = make_comment(31, post_id=7, author_id=9)
    session = FakeSession(
        gets={(Post, 7): post, (User, 1): make_user(1), (Course, 5): make_course()},
        results=[
            FakeResult(scalar=3),                          # 投票总分
            FakeResult(rows=[(comment, make_user(9))]),    # 评论
            FakeResult(rows=[(31, 5)]),                    # 评论投票聚合
        ],
    )
    with http_client(session, None) as client:
        resp = client.get("/posts/7")
    assert resp.status_code == 200
    assert post.view_count == 11
    assert "AI 助教正在思考" in resp.text  # pending 轮询卡
    assert "评论内容" in resp.text


def test_post_detail_qa_done_with_citations(monkeypatch):
    monkeypatch.setattr(pages, "get_redis", lambda: FakeRedis())
    citations = [{"chunk_id": "pub_d1_00001", "document_id": 3, "chapter": "第 1 章",
                  "section": "1.1", "page_num": 3, "uploader": "用户9"}]
    monkeypatch.setattr(pages, "build_ai_citations", async_value(citations))
    post = make_post(7, board="qa", ai_first_answer="根据 [pub_d1_00001] 可知")
    session = FakeSession(
        gets={(Post, 7): post, (User, 1): make_user(1), (Course, 5): make_course()},
        results=[
            FakeResult(scalar=0),     # 投票总分
            FakeResult(rows=[]),      # 无评论
            FakeResult(scalar=None),  # my_vote
            FakeResult(scalar=None),  # is_favorited
        ],
    )
    with http_client(session, USER) as client:
        resp = client.get("/posts/7")
    assert resp.status_code == 200
    assert "AI 助教首答" in resp.text
    assert "/documents/3/source#pub_d1_00001" in resp.text  # 引用切片渲染溯源链接


def test_post_detail_experience_summary_done():
    post = make_post(7, board="experience", ai_summary="这是 AI 摘要")
    session = FakeSession(
        gets={(Post, 7): post, (User, 1): make_user(1), (Course, 5): make_course()},
        results=[FakeResult(scalar=0), FakeResult(rows=[])],
    )
    with http_client(session, None) as client:
        resp = client.get("/posts/7")
    assert resp.status_code == 200
    assert "这是 AI 摘要" in resp.text


# ---------------------------------------------------------------------------
# 个人中心 / 全站搜索
# ---------------------------------------------------------------------------


def test_me_page_renders_growth():
    with http_client(FakeSession(), None) as client:
        resp = client.get("/me", follow_redirects=False)
        assert resp.status_code == 303
    session = FakeSession(gets={(Major, 3): make_major()})
    with http_client(session, USER) as client:
        resp = client.get("/me")
    assert resp.status_code == 200


def test_search_page_sanitizes_params():
    with http_client(FakeSession(), None) as client:
        resp = client.get("/search", follow_redirects=False)
        assert resp.status_code == 303
    with http_client(user=USER) as client:
        resp = client.get("/search", params={"q": "死锁", "type": "post", "board": "qa"})
        assert resp.status_code == 200 and "死锁" in resp.text
        # 非法 type/board 回落默认值，页面照常渲染
        resp = client.get("/search", params={"type": "bad", "board": "bad"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 补充分支：过滤参数 / 评论树采纳置顶 / 等级封顶 / 摘要状态轮询
# ---------------------------------------------------------------------------


def test_board_page_grade_and_featured_filters():
    post = make_post(101, board="experience", status="featured", ai_summary="摘要")
    author = make_user(9, grade="2023级")
    session = FakeSession(results=[
        FakeResult(scalar=1),
        FakeResult(rows=[(post, author)]),
        FakeResult(rows=[]),
        FakeResult(rows=[]),
        FakeResult(rows=[["保研"]]),
        FakeResult(rows=["2023级"]),
    ])
    with http_client(session, None) as client:
        resp = client.get("/boards/experience", params={"grade": "2023级", "featured": "1"})
    assert resp.status_code == 200 and "测试帖子" in resp.text


def test_post_detail_logged_in_votes_and_accepted_pinning():
    post = make_post(7, board="qa", ai_first_answer="已有答案", accepted_comment_id=31)
    comment = make_comment(31, post_id=7, author_id=9, is_accepted=True)
    session = FakeSession(
        gets={(Post, 7): post, (User, 1): make_user(1), (Course, 5): make_course()},
        results=[
            FakeResult(scalar=3),                          # 帖子投票总分
            FakeResult(rows=[(comment, make_user(9))]),    # 评论
            FakeResult(rows=[(31, 5)]),                    # 评论投票聚合
            FakeResult(scalar=1),                          # 我的帖子投票
            FakeResult(rows=[(31, -1)]),                   # 我的评论投票
            FakeResult(scalar=None),                       # is_favorited
        ],
    )
    with http_client(session, USER) as client:
        resp = client.get("/posts/7")
    assert resp.status_code == 200
    assert "已采纳" in resp.text  # 采纳评论置顶渲染


def test_post_detail_unknown_citation_falls_back_to_text(monkeypatch):
    # 引用 ID 不在溯源表内：跳过切片，整段按纯文本渲染
    monkeypatch.setattr(pages, "build_ai_citations", async_value([]))
    post = make_post(7, board="qa", ai_first_answer="见 [pub_d9_99999] 说明")
    session = FakeSession(
        gets={(Post, 7): post, (User, 1): make_user(1), (Course, 5): make_course()},
        results=[FakeResult(scalar=0), FakeResult(rows=[])],
    )
    with http_client(session, None) as client:
        resp = client.get("/posts/7")
    assert resp.status_code == 200
    assert "pub_d9_99999" in resp.text


def test_post_detail_experience_summary_failed(monkeypatch):
    redis = FakeRedis()
    redis.kv[pages.exp_summary_key(7)] = "failed"
    monkeypatch.setattr(pages, "get_redis", lambda: redis)
    post = make_post(7, board="experience", ai_summary=None)
    session = FakeSession(
        gets={(Post, 7): post, (User, 1): make_user(1), (Course, 5): make_course()},
        results=[FakeResult(scalar=0), FakeResult(rows=[])],
    )
    with http_client(session, None) as client:
        resp = client.get("/posts/7")
    assert resp.status_code == 200
    assert "AI 摘要生成失败" in resp.text


def test_me_page_max_level():
    top_user = make_user(1, level=6, score=3000)
    session = FakeSession(gets={(Major, 3): make_major()})
    with http_client(session, top_user) as client:
        resp = client.get("/me")
    assert resp.status_code == 200


def test_post_detail_tolerates_redis_failure(monkeypatch):
    """Redis 宕机：AI 首答状态键读取失败仅记日志，详情页仍 200（按无状态键处理）。"""

    def _broken():
        raise ConnectionError("redis down")

    monkeypatch.setattr(pages, "get_redis", _broken)
    post = make_post(7, board="qa", ai_first_answer=None)
    session = FakeSession(
        gets={(Post, 7): post, (User, 1): make_user(1), (Course, 5): make_course()},
        results=[FakeResult(scalar=0), FakeResult(rows=[])],
    )
    with http_client(session, None) as client:
        resp = client.get("/posts/7")
    assert resp.status_code == 200
    assert "AI 助教正在思考" not in resp.text  # 无状态键：不渲染 pending 轮询卡
