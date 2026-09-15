"""骨架冒烟测试：应用可创建、健康检查可用（§18.1）。"""

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
