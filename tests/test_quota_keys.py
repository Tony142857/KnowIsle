"""v0.5 AI 额度兑换 + 自定义 LLM Key 单元测试（§10.1）。

CI 环境无 Postgres/Redis 服务：Fernet 加解密、额度扣减顺序、模型回退规则均走
纯函数 / 内存替身测试，不触碰真实存储；upsert / 兑换等 DB 链路在容器 compose
环境内验证（见开发进度文档）。
"""

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.core.llm import router as llm_router
from app.core.llm.router import ModelTier
from app.identity.llm_keys import decrypt_api_key, encrypt_api_key
from app.identity.quota import apply_consume, can_answer

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
