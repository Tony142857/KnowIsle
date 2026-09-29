"""骨架冒烟测试：应用可创建、健康检查与基础布局可用（§18.1）。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_index_page():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "知屿" in resp.text
    assert "课程空间" in resp.text  # 板块总览卡片渲染


def test_login_page():
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "学号" in resp.text


def test_board_pages_render_without_db():
    # v0.5：qa/discuss 板块页已交付（SSR 骨架 + 列表降级客户端加载），匿名可读
    for path, name in (("/boards/qa", "知屿问答"), ("/boards/discuss", "讨论区")):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert name in resp.text

    # experience/bounty 仍为占位页
    for path in ("/boards/experience", "/boards/bounty"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "建设中" in resp.text


def test_me_and_post_new_require_login():
    # v0.5：个人中心 / 发帖页需登录，未登录 303 重定向到 /login
    for path in ("/me", "/posts/new"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/login"


def test_library_pages_require_login():
    # v0.3：个人知识库 / AI 对话 / 原文溯源 均需登录，未登录 303 重定向到 /login
    for path in ("/library", "/library/chat?course_id=1", "/documents/1/source"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/login"


def test_review_pages_require_login():
    # v0.4：协审工作台 / 管理后台·审核 / 资源详情页 需登录，未登录 303 重定向到 /login
    for path in ("/review", "/admin/review", "/resources/1"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/login"


def test_moderation_apis_require_login():
    # v0.4：协审任务 / 公共资源 / 投稿 API 未登录一律 401
    resp = client.get("/api/review/tasks")
    assert resp.status_code == 401
    resp = client.get("/api/resources")
    assert resp.status_code == 401
    resp = client.post("/api/documents/1/submit", json={})
    assert resp.status_code == 401


def test_invalid_board_returns_404_html():
    resp = client.get("/boards/not-a-board")
    assert resp.status_code == 404
    assert "text/html" in resp.headers["content-type"]


def test_api_404_returns_json():
    resp = client.get("/api/no-such-endpoint")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


def test_unread_count_anonymous():
    # v0.5：未读角标接入登录态，匿名访问返回 0
    resp = client.get("/api/notifications/unread-count")
    assert resp.status_code == 200
    assert resp.json() == {"unread": 0}
