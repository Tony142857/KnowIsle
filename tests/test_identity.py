"""v0.2 认证与权限单元测试（§18.1）。

CI 环境无 Postgres/Redis 服务，本文件全部使用内存替身（FakeRedis / 假用户对象），
不触碰真实存储；端到端链路在容器 compose 环境内验证（见开发进度文档）。
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.identity import email_fallback
from app.identity.rbac import require_admin, require_builder, require_reviewer
from app.identity.session import create_session, destroy_session, issue_jwt, resolve_session, verify_jwt


class FakeRedis:
    """最小内存 Redis 替身：支持 set/get/delete/expire/ttl（不模拟真实时间流逝）。"""

    def __init__(self):
        self._store: dict[str, tuple[str, int | None]] = {}

    async def set(self, key, value, ex=None):
        self._store[key] = (str(value), ex)

    async def get(self, key):
        item = self._store.get(key)
        return item[0] if item else None

    async def delete(self, key):
        self._store.pop(key, None)

    async def expire(self, key, seconds):
        if key in self._store:
            value, _ = self._store[key]
            self._store[key] = (value, seconds)

    async def ttl(self, key):
        if key not in self._store:
            return -2
        return self._store[key][1] or -1


# ---------------------------------------------------------------------------
# Redis Session（identity/session.py）
# ---------------------------------------------------------------------------


async def test_session_roundtrip():
    redis = FakeRedis()
    token = await create_session(redis, user_id=42)
    assert await resolve_session(redis, token) == 42
    await destroy_session(redis, token)
    assert await resolve_session(redis, token) is None


async def test_resolve_invalid_session():
    assert await resolve_session(FakeRedis(), "no-such-token") is None
    assert await resolve_session(FakeRedis(), "") is None


# ---------------------------------------------------------------------------
# JWT（API 通道，§7.4）
# ---------------------------------------------------------------------------


def test_jwt_roundtrip():
    token = issue_jwt(7)
    assert verify_jwt(token) == 7


def test_jwt_tampered_rejected():
    token = issue_jwt(7) + "x"
    assert verify_jwt(token) is None


def test_jwt_garbage_rejected():
    assert verify_jwt("not-a-jwt") is None


# ---------------------------------------------------------------------------
# 邮箱验证码（identity/email_fallback.py，开发期假通道）
# ---------------------------------------------------------------------------


async def test_email_code_flow():
    redis = FakeRedis()
    dev_code, cooldown = await email_fallback.send_code(redis, "20230001", "a@stu.edu.cn")
    assert dev_code is not None and len(dev_code) == 6 and cooldown == 0
    # 验证通过且一次性使用
    assert await email_fallback.verify_code(redis, "20230001", dev_code) is True
    assert await email_fallback.verify_code(redis, "20230001", dev_code) is False


async def test_email_code_cooldown():
    redis = FakeRedis()
    await email_fallback.send_code(redis, "20230002", "b@stu.edu.cn")
    dev_code, cooldown = await email_fallback.send_code(redis, "20230002", "b@stu.edu.cn")
    assert dev_code is None and cooldown > 0  # 冷却期内重发被拦截


async def test_email_code_wrong_rejected():
    redis = FakeRedis()
    await email_fallback.send_code(redis, "20230003", "c@stu.edu.cn")
    wrong = "000000"  # 可能恰与真实验证码相同的概率可忽略；错误码不得通过
    ok = await email_fallback.verify_code(redis, "20230003", wrong)
    assert ok is False


async def test_email_code_peek_does_not_consume():
    """「需补全资料」中间态只校验不销毁，正式登录时才一次性消耗。"""
    redis = FakeRedis()
    dev_code, _ = await email_fallback.send_code(redis, "20230004", "d@stu.edu.cn")
    assert await email_fallback.verify_code(redis, "20230004", dev_code, consume=False) is True
    assert await email_fallback.verify_code(redis, "20230004", dev_code) is True
    assert await email_fallback.verify_code(redis, "20230004", dev_code) is False


# ---------------------------------------------------------------------------
# RBAC 角色门控（identity/rbac.py，§3.2：越权 404；admin 放行所有门控）
# ---------------------------------------------------------------------------


def _user(role: str):
    return SimpleNamespace(id=1, role=role, status="active")


async def test_require_admin():
    dep = require_admin
    assert await dep(user=_user("admin")) is not None
    for role in ("student", "reviewer", "builder"):
        with pytest.raises(HTTPException) as exc:
            await dep(user=_user(role))
        assert exc.value.status_code == 404


async def test_parallel_roles_not_interchangeable():
    """reviewer 与 builder 为平行特权角色，互不蕴含。"""
    with pytest.raises(HTTPException):
        await require_reviewer(user=_user("builder"))
    with pytest.raises(HTTPException):
        await require_builder(user=_user("reviewer"))
    # admin 为最高角色，放行所有门控
    assert await require_reviewer(user=_user("admin")) is not None
    assert await require_builder(user=_user("admin")) is not None


def test_require_role_rejects_unknown_role():
    from app.identity.rbac import require_role

    with pytest.raises(ValueError):
        require_role("superuser")
