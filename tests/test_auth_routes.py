"""认证首次建档并发与管理员信用裁决行锁回归测试。"""

from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError

from app.api import admin as admin_api
from app.api import auth as auth_api
from app.identity import email_fallback
from app.storage.models import User

from .fakestack import FakeRedis, FakeResult, FakeSession, async_value, http_client, make_user


class _RaceFlushSession(FakeSession):
    async def flush(self):
        if self.rollbacks == 0:
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        await super().flush()


class _RaceCommitSession(FakeSession):
    async def commit(self):
        if self.commits == 0:
            self.commits += 1
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        await super().commit()


class _GetSpySession(FakeSession):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.get_calls = []

    async def get(self, model, pk, **kwargs):
        self.get_calls.append((model, pk, kwargs))
        return await super().get(model, pk, **kwargs)


def _profile_payload(nickname: str) -> dict:
    return {
        "student_no": "20239999",
        "email": "student@stu.edu.cn",
        "code": "123456",
        "real_name": "张三",
        "nickname": nickname,
        "next": "/me",
    }


async def _patch_redis(monkeypatch):
    redis = FakeRedis()
    await redis.set("email_code:20239999", "123456", ex=300)
    monkeypatch.setattr(auth_api, "get_redis", lambda: redis)
    return redis


async def test_student_no_race_logs_in_existing_user(monkeypatch):
    redis = await _patch_redis(monkeypatch)
    winner = make_user(9, student_no="20239999", nickname="先建档")
    identity = SimpleNamespace(raw_profile={"email": "student@stu.edu.cn"})
    session = _RaceFlushSession(results=[
        FakeResult(scalar=None),       # 首次按学号查询
        FakeResult(scalar=None),       # 昵称预查
        FakeResult(scalar=winner),     # 冲突回滚后按学号重查
        FakeResult(scalar=identity),   # 老用户邮箱绑定查询
    ])
    with http_client(session) as client:
        resp = client.post("/api/auth/email/verify", json=_profile_payload("后建档"))
    assert resp.status_code == 200
    assert resp.json() == {
        "need_profile": False,
        "user": {
            "id": 9,
            "student_no": "20239999",
            "nickname": "先建档",
            "avatar_url": None,
            "role": "student",
        },
        "next": "/me",
    }
    assert session.rollbacks == 1
    assert await email_fallback.verify_code(redis, "20239999", "123456", consume=False) is False


async def test_student_no_race_still_checks_bound_email(monkeypatch):
    redis = await _patch_redis(monkeypatch)
    winner = make_user(9, student_no="20239999", nickname="先建档")
    identity = SimpleNamespace(raw_profile={"email": "other@stu.edu.cn"})
    session = _RaceFlushSession(results=[
        FakeResult(scalar=None),
        FakeResult(scalar=None),
        FakeResult(scalar=winner),
        FakeResult(scalar=identity),
    ])
    with http_client(session) as client:
        resp = client.post("/api/auth/email/verify", json=_profile_payload("后建档"))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "学号与绑定邮箱不一致"
    assert await email_fallback.verify_code(redis, "20239999", "123456", consume=False) is True


async def test_nickname_race_returns_422_without_consuming_code(monkeypatch):
    redis = await _patch_redis(monkeypatch)
    session = _RaceCommitSession(results=[
        FakeResult(scalar=None),   # 首次按学号查询
        FakeResult(scalar=None),   # 昵称预查
        FakeResult(scalar=None),   # 冲突回滚后仍无同学号用户
        FakeResult(scalar=9),      # 对方已占用昵称
    ])
    with http_client(session) as client:
        resp = client.post("/api/auth/email/verify", json=_profile_payload("重复昵称"))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "昵称已被使用"
    assert session.rollbacks == 1
    assert await email_fallback.verify_code(redis, "20239999", "123456", consume=False) is True


def test_adjust_credit_locks_and_refreshes_target(monkeypatch):
    monkeypatch.setattr(admin_api, "apply_credit_change", async_value(85))
    target = make_user(9)
    session = _GetSpySession(gets={(User, 9): target})
    with http_client(session, make_user(1, role="admin")) as client:
        resp = client.post(
            "/api/admin/users/9/credit", json={"delta": -15, "reason": "违规"}
        )
    assert resp.status_code == 200
    assert session.get_calls == [
        (User, 9, {"with_for_update": True, "populate_existing": True})
    ]
