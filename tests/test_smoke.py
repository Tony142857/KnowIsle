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


def test_placeholder_pages():
    # /majors、/library 已交付为真实页面（需数据库，不在冒烟范围内）
    for path in ("/me", "/boards/qa", "/boards/experience"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "建设中" in resp.text


def test_library_pages_require_login():
    # v0.3：个人知识库 / AI 对话 / 原文溯源 均需登录，未登录 303 重定向到 /login
    for path in ("/library", "/library/chat?course_id=1", "/documents/1/source"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/login"


def test_invalid_board_returns_404_html():
    resp = client.get("/boards/not-a-board")
    assert resp.status_code == 404
    assert "text/html" in resp.headers["content-type"]


def test_api_404_returns_json():
    resp = client.get("/api/no-such-endpoint")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


def test_unread_count_stub():
    resp = client.get("/api/notifications/unread-count")
    assert resp.status_code == 200
    assert resp.json() == {"unread": 0}
