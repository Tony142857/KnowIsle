"""动态流接口测试（§13，v0.9）：GET /api/feed 的匿名语义 / 关注课程新资料 / 热帖聚合排序。

FakeSession 结果按序出队：登录用户先出「关注课程 ID」、有关注才出「上架资源」，
随后是热帖链（帖子候选 → 有候选才出评论数/投票分聚合）。
"""

from datetime import timedelta

from .fakestack import (
    NOW,
    FakeResult,
    FakeSession,
    http_client,
    make_post,
    make_resource,
    make_user,
)

USER = make_user(1)


def _hot_results(posts, comment_rows=(), vote_rows=()):
    """热帖查询链的结果队列：无候选帖子时不产生聚合查询。"""
    results = [FakeResult(rows=posts)]
    if posts:
        results += [FakeResult(rows=comment_rows), FakeResult(rows=vote_rows)]
    return results


def test_feed_anonymous_returns_only_hot_posts():
    # 匿名语义：200 且 new_resources 恒为空（页面侧渲染登录引导卡），不 401
    post = make_post(101, board="qa")
    session = FakeSession(results=_hot_results([post], [(101, 3)], [(101, 7)]))
    with http_client(session) as client:
        resp = client.get("/api/feed")
    assert resp.status_code == 200
    data = resp.json()
    assert data["new_resources"] == []
    assert len(data["hot_posts"]) == 1
    item = data["hot_posts"][0]
    assert item["title"] == "测试帖子"
    assert item["board"] == "qa" and item["board_name"] == "知屿问答"
    assert item["score"] == 7 and item["comment_count"] == 3
    assert item["url"] == "/posts/101"
    assert "created_at" in item


def test_feed_logged_in_without_follows():
    # 未关注任何课程：不查资源表，new_resources 为空；热帖照常
    post = make_post(101, board="experience")
    session = FakeSession(
        results=[FakeResult(rows=[]), *_hot_results([post], [], [(101, 2)])]
    )
    with http_client(session, USER) as client:
        data = client.get("/api/feed").json()
    assert data["new_resources"] == []
    assert len(data["hot_posts"]) == 1


def test_feed_logged_in_with_followed_course_resources():
    resource = make_resource(21, title="操作系统笔记")
    session = FakeSession(results=[
        FakeResult(rows=[5]),                        # 关注的课程 ID
        FakeResult(rows=[(resource, "操作系统")]),   # 关注课程的上架资源
        *_hot_results([]),
    ])
    with http_client(session, USER) as client:
        data = client.get("/api/feed").json()
    assert data["hot_posts"] == []
    assert len(data["new_resources"]) == 1
    item = data["new_resources"][0]
    assert item["title"] == "操作系统笔记"
    assert item["course_id"] == 5 and item["course_name"] == "操作系统"
    assert item["url"] == "/resources/21"
    assert "created_at" in item


def test_feed_hot_posts_sorted_by_score_then_time():
    # 投票分倒序；同分按发布时间倒序
    old = make_post(101, title="旧热帖", created_at=NOW - timedelta(days=1))
    high = make_post(102, title="高分帖", created_at=NOW - timedelta(days=2))
    newer = make_post(103, title="同分新帖")
    session = FakeSession(results=_hot_results(
        [old, high, newer],
        [(102, 5)],
        [(101, 3), (102, 9), (103, 3)],
    ))
    with http_client(session) as client:
        posts = client.get("/api/feed").json()["hot_posts"]
    assert [p["id"] for p in posts] == [102, 103, 101]
    assert posts[0]["comment_count"] == 5
    assert posts[1]["comment_count"] == 0  # 无评论聚合行时回落 0
