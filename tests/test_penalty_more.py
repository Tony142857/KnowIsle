"""v0.8 门控补充测试：assert_can_speak 禁言拦截 / 到期惰性解除，
check_action_cooldown 低信用冷却各分支（放行 / 命中 / 429 / Redis 故障容错）。

与 test_penalty.py 同一约定：FakeRedis 内存替身 + monkeypatch，不触碰真实 Redis。
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.identity import penalty
from app.identity.growth import TIER_RATE_LIMIT
from app.identity.penalty import (
    assert_can_speak,
    check_action_cooldown,
    cooldown_key,
)

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


class FakeCooldownRedis:
    """最小冷却 Redis 替身：set 返回预设的 NX 结果，pttl 返回预设毫秒。"""

    def __init__(self, acquired: bool = True, pttl: int = 5000):
        self._acquired = acquired
        self._pttl = pttl
        self.set_calls: list = []

    async def set(self, key, value, nx=False, ex=None):
        self.set_calls.append((key, value, nx, ex))
        return self._acquired

    async def pttl(self, _key):
        return self._pttl


def _user(status="active", muted_until=None, credit=100):
    return SimpleNamespace(id=7, status=status, muted_until=muted_until, credit=credit)


# ---------------------------------------------------------------------------
# assert_can_speak：禁言拦截与到期惰性解除
# ---------------------------------------------------------------------------


def test_assert_can_speak_active_and_frozen_passthrough():
    """非 muted 状态直接放行（frozen 由 rbac 登录态拦截，此处不处理）。"""
    for status in ("active", "frozen"):
        user = _user(status=status)
        assert_can_speak(user, NOW)
        assert user.status == status


def test_assert_can_speak_muted_not_expired_raises():
    user = _user(status="muted", muted_until=NOW + timedelta(days=3))
    with pytest.raises(HTTPException) as exc_info:
        assert_can_speak(user, NOW)
    assert exc_info.value.status_code == 403
    assert (NOW + timedelta(days=3)).isoformat() in exc_info.value.detail
    assert user.status == "muted"  # 未到期不做任何变更


def test_assert_can_speak_muted_without_deadline_raises():
    """muted_until 为 NULL 时按无期限禁言处理，给通用文案。"""
    user = _user(status="muted", muted_until=None)
    with pytest.raises(HTTPException) as exc_info:
        assert_can_speak(user, NOW)
    assert exc_info.value.status_code == 403
    assert "禁言" in exc_info.value.detail


def test_assert_can_speak_muted_expired_lazy_unmute():
    """禁言已到期：放行并惰性解除（status→active、muted_until 清空，调用方 commit）。"""
    user = _user(status="muted", muted_until=NOW - timedelta(seconds=1))
    assert_can_speak(user, NOW)
    assert user.status == "active" and user.muted_until is None


def test_assert_can_speak_muted_expiring_exactly_now_unmutes():
    """muted_until == now 视为到期（<= 判定）。"""
    user = _user(status="muted", muted_until=NOW)
    assert_can_speak(user, NOW)
    assert user.status == "active"


# ---------------------------------------------------------------------------
# check_action_cooldown：credit < 80 行为冷却
# ---------------------------------------------------------------------------


async def test_cooldown_skipped_for_normal_credit(monkeypatch):
    """credit >= 80 直接放行，不触碰 Redis。"""
    monkeypatch.setattr(penalty, "get_redis", lambda: (_ for _ in ()).throw(AssertionError))
    await check_action_cooldown(_user(credit=TIER_RATE_LIMIT), "post", 60)


async def test_cooldown_first_action_acquired(monkeypatch):
    """冷却窗口内首次操作：SET NX 成功，放行；键含行为与用户维度。"""
    redis = FakeCooldownRedis(acquired=True)
    monkeypatch.setattr(penalty, "get_redis", lambda: redis)
    await check_action_cooldown(_user(credit=50), "post", 60)
    assert redis.set_calls == [(cooldown_key("post", 7), "1", True, 60)]


async def test_cooldown_repeated_action_429_with_remaining(monkeypatch):
    """冷却未过期再操作：429，detail 含剩余秒数（pttl 向上取整）。"""
    redis = FakeCooldownRedis(acquired=False, pttl=4500)
    monkeypatch.setattr(penalty, "get_redis", lambda: redis)
    with pytest.raises(HTTPException) as exc_info:
        await check_action_cooldown(_user(credit=79), "comment", 60)
    assert exc_info.value.status_code == 429
    assert str(TIER_RATE_LIMIT) in exc_info.value.detail
    assert "请 5 秒后重试" in exc_info.value.detail  # (4500+999)//1000


async def test_cooldown_429_falls_back_to_full_window_when_pttl_missing(monkeypatch):
    """键存在但 pttl 已失效（≤0）：提示秒数回落为整个冷却窗口。"""
    redis = FakeCooldownRedis(acquired=False, pttl=-1)
    monkeypatch.setattr(penalty, "get_redis", lambda: redis)
    with pytest.raises(HTTPException) as exc_info:
        await check_action_cooldown(_user(credit=10), "upload", 90)
    assert exc_info.value.status_code == 429
    assert "请 90 秒后重试" in exc_info.value.detail


async def test_cooldown_tolerates_redis_failure(monkeypatch):
    """get_redis 故障：仅记日志放行，不阻塞主流程。"""
    monkeypatch.setattr(
        penalty, "get_redis", lambda: (_ for _ in ()).throw(ConnectionError("redis down"))
    )
    await check_action_cooldown(_user(credit=0), "post", 60)


async def test_cooldown_tolerates_redis_op_failure(monkeypatch):
    """SET 调用本身故障同样放行。"""

    class BrokenRedis:
        async def set(self, *_args, **_kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr(penalty, "get_redis", BrokenRedis)
    await check_action_cooldown(_user(credit=0), "post", 60)
