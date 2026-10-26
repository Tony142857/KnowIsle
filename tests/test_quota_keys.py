"""v0.5 AI 额度兑换 + 自定义 LLM Key 单元测试（§10.1）。

CI 环境无 Postgres/Redis 服务：Fernet 加解密、额度扣减顺序、模型回退规则均走
纯函数 / 内存替身测试，不触碰真实存储；upsert / 兑换等 DB 链路在容器 compose
环境内验证（见开发进度文档）。
"""

from datetime import date
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.core.llm import router as llm_router
from app.core.llm.router import ModelTier
from app.identity import quota
from app.identity.llm_keys import decrypt_api_key, encrypt_api_key
from app.identity.quota import (
    apply_consume,
    can_answer,
    check_quota,
    consume_quota,
    ensure_quota,
)
from app.storage.models import AiQuota

# ---------------------------------------------------------------------------
# Fernet 加解密（identity/llm_keys.py）
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_roundtrip():
    enc = encrypt_api_key("sk-test-123456")
    assert decrypt_api_key(enc) == "sk-test-123456"


def test_ciphertext_not_contains_plaintext():
    plain = "sk-super-secret-key"
    enc = encrypt_api_key(plain)
    assert plain not in enc
    assert enc != plain


def test_decrypt_with_wrong_key_fails():
    """密钥轮换后旧密文无法解密（路由层据此降级官方通道）。"""
    other = Fernet(Fernet.generate_key())
    enc = other.encrypt(b"sk-abc").decode()
    with pytest.raises(InvalidToken):
        decrypt_api_key(enc)


def test_decrypt_garbage_fails():
    with pytest.raises(InvalidToken):
        decrypt_api_key("not-a-fernet-token")


# ---------------------------------------------------------------------------
# 额度扣减顺序（identity/quota.py 纯函数）
# ---------------------------------------------------------------------------


def test_can_answer_free_remaining():
    assert can_answer(used=5, daily_limit=30, bonus_balance=0) is True


def test_can_answer_free_used_up_with_bonus():
    assert can_answer(used=30, daily_limit=30, bonus_balance=3) is True


def test_can_answer_blocked_when_both_exhausted():
    assert can_answer(used=30, daily_limit=30, bonus_balance=0) is False


def test_apply_consume_prefers_free_quota():
    """免费额度未用尽时只动 used，不动 bonus_balance。"""
    assert apply_consume(used=0, daily_limit=30, bonus_balance=5) == (1, 5)


def test_apply_consume_bonus_after_free_exhausted():
    """免费额度用尽后扣兑换余额。"""
    assert apply_consume(used=30, daily_limit=30, bonus_balance=5) == (30, 4)


# ---------------------------------------------------------------------------
# 自定义 Key 模型回退规则（core/llm/router.py）
# ---------------------------------------------------------------------------


def _key(**fields):
    defaults = {"model_short": None, "model_medium": None, "model_long": None}
    return SimpleNamespace(**(defaults | fields))


def test_pick_model_tier_field_first():
    key = _key(model_short="m-s", model_medium="m-med")
    assert llm_router._pick_model(key, ModelTier.MEDIUM) == "m-med"


def test_pick_model_falls_back_to_short():
    key = _key(model_short="m-s")
    assert llm_router._pick_model(key, ModelTier.MEDIUM) == "m-s"
    assert llm_router._pick_model(key, ModelTier.LONG) == "m-s"


def test_pick_model_falls_back_to_official_default():
    key = _key()
    assert llm_router._pick_model(key, ModelTier.SHORT) == llm_router._official_model(
        ModelTier.SHORT
    )
    assert llm_router._pick_model(key, ModelTier.LONG) == llm_router._official_model(
        ModelTier.LONG
    )


# ---------------------------------------------------------------------------
# get_user_client：解密失败降级（monkeypatch 掉 DB 查询，不触碰真实存储）
# ---------------------------------------------------------------------------


def _patch_key(monkeypatch, key):
    async def fake_get_user_key(session, user_id):
        return key

    monkeypatch.setattr(llm_router, "get_user_key", fake_get_user_key)


async def test_get_user_client_success(monkeypatch):
    key = _key(
        base_url="https://api.example.com/v1/",
        api_key_enc=encrypt_api_key("sk-user-ok"),
        model_short="my-model",
    )
    _patch_key(monkeypatch, key)
    client = await llm_router.get_user_client(None, 1, ModelTier.MEDIUM)
    assert client is not None
    assert client.base_url == "https://api.example.com/v1"  # 尾部斜杠被规整
    assert client.api_key == "sk-user-ok"
    assert client.model == "my-model"  # medium 为空回退 model_short


async def test_get_user_client_decrypt_failure_returns_none(monkeypatch):
    """密文用别的密钥加密（模拟 SECRET_KEY 轮换）→ 返回 None 降级官方通道。"""
    key = _key(
        base_url="https://api.example.com/v1",
        api_key_enc=Fernet(Fernet.generate_key()).encrypt(b"sk-x").decode(),
    )
    _patch_key(monkeypatch, key)
    assert await llm_router.get_user_client(None, 1, ModelTier.SHORT) is None


async def test_get_user_client_no_record_returns_none(monkeypatch):
    _patch_key(monkeypatch, None)
    assert await llm_router.get_user_client(None, 1, ModelTier.SHORT) is None


# ---------------------------------------------------------------------------
# 额度 DB 链路（ensure / check / consume，identity/quota.py）：FakeSession 内存替身
# §20.5 B1：consume_quota 为行锁内「检查+扣减」一体的原子操作
# ---------------------------------------------------------------------------


class _QuotaResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row

    def scalar_one(self):
        return self._row


class FakeQuotaSession:
    """最小 AsyncSession 替身：execute 按序出队预置额度行，add/flush 记录副作用。

    fail_flush=True 时首次 flush 抛 IntegrityError（模拟并发建行撞唯一约束）。
    """

    def __init__(self, row=None, rows=None, fail_flush=False):
        self._rows = list(rows) if rows is not None else [row]
        self._fail_flush = fail_flush
        self.added: list = []
        self.flushed = False
        self.expunged: list = []

    async def execute(self, _stmt):
        return _QuotaResult(self._rows.pop(0))

    def add(self, obj):
        self.added.append(obj)

    def expunge(self, obj):
        self.expunged.append(obj)
        if obj in self.added:
            self.added.remove(obj)

    def begin_nested(self):
        class _Nested:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, *_exc):
                return False

        return _Nested()

    async def flush(self):
        if self._fail_flush:
            self._fail_flush = False
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        self.flushed = True


async def _fake_daily_limit(_session, key):
    assert key == "ai_daily_limit"
    return 30


async def test_ensure_quota_returns_existing_row():
    """当日已有额度行：直接返回，不新建、不 flush。"""
    row = SimpleNamespace(used=5, daily_limit=30, bonus_balance=2)
    session = FakeQuotaSession(row)
    assert await ensure_quota(session, SimpleNamespace(id=7)) is row
    assert session.added == [] and session.flushed is False


async def test_ensure_quota_creates_row(monkeypatch):
    """当日无额度行：按平台配置建当日行（used=0），只 flush 不 commit。"""
    monkeypatch.setattr(quota, "get_config", _fake_daily_limit)
    session = FakeQuotaSession(None)
    row = await ensure_quota(session, SimpleNamespace(id=7))
    assert session.added == [row] and session.flushed is True
    assert isinstance(row, AiQuota)
    assert row.user_id == 7 and row.date == date.today()
    assert row.used == 0 and row.daily_limit == 30


async def test_check_quota_missing_row_is_read_only(monkeypatch):
    monkeypatch.setattr(quota, "get_config", _fake_daily_limit)
    session = FakeQuotaSession(None)
    await check_quota(session, SimpleNamespace(id=7))
    assert session.added == [] and session.flushed is False


async def test_check_quota_passes_with_free_remaining():
    row = SimpleNamespace(used=5, daily_limit=30, bonus_balance=0)
    await check_quota(FakeQuotaSession(row), SimpleNamespace(id=7))


async def test_check_quota_passes_with_bonus_after_free_used_up():
    row = SimpleNamespace(used=30, daily_limit=30, bonus_balance=2)
    await check_quota(FakeQuotaSession(row), SimpleNamespace(id=7))


async def test_check_quota_429_when_both_exhausted():
    """免费额度与兑换余额皆尽：429，提示兑换或自定义 Key。"""
    row = SimpleNamespace(used=30, daily_limit=30, bonus_balance=0)
    with pytest.raises(HTTPException) as exc_info:
        await check_quota(FakeQuotaSession(row), SimpleNamespace(id=7))
    assert exc_info.value.status_code == 429
    assert "额度已用完" in exc_info.value.detail


async def test_consume_quota_prefers_free_quota():
    row = SimpleNamespace(used=5, daily_limit=30, bonus_balance=2)
    await consume_quota(FakeQuotaSession(row), SimpleNamespace(id=7))
    assert row.used == 6 and row.bonus_balance == 2


async def test_consume_quota_deducts_bonus_after_free_exhausted():
    row = SimpleNamespace(used=30, daily_limit=30, bonus_balance=2)
    await consume_quota(FakeQuotaSession(row), SimpleNamespace(id=7))
    assert row.used == 30 and row.bonus_balance == 1


async def test_consume_quota_429_when_exhausted():
    """§20.5 B1：原子扣减在锁内复检——预检后额度被并发耗尽时抛 429，不超扣。"""
    row = SimpleNamespace(used=30, daily_limit=30, bonus_balance=0)
    with pytest.raises(HTTPException) as exc_info:
        await consume_quota(FakeQuotaSession(row), SimpleNamespace(id=7))
    assert exc_info.value.status_code == 429
    assert (row.used, row.bonus_balance) == (30, 0)  # 未扣减


async def test_consume_quota_concurrent_row_creation(monkeypatch):
    """§20.5 B1：并发建行撞唯一约束 → SAVEPOINT 回退 + expunge 本地插入，
    加锁重读对方已建行后正常扣减。"""
    monkeypatch.setattr(quota, "get_config", _fake_daily_limit)
    winner = SimpleNamespace(used=5, daily_limit=30, bonus_balance=0)
    session = FakeQuotaSession(rows=[None, winner], fail_flush=True)
    await consume_quota(session, SimpleNamespace(id=7))
    assert winner.used == 6  # 扣在对方建的行上
    assert len(session.expunged) == 1  # 本地 pending 插入已回退
