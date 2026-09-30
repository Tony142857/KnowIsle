"""用户自定义 LLM Key（§10.1）：Fernet 对称加密存储与 CRUD。

密钥由 SECRET_KEY 派生（SHA-256 → urlsafe base64），与 JWT 签名同源；
仅后端调用时解密，前端永不回显 api_key。写操作只 flush，提交由调用方负责。
"""

import base64
import hashlib
from datetime import UTC, datetime

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.storage.models import UserLlmKey

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """由 settings.secret_key 派生 Fernet 实例（模块级缓存）。"""
    global _fernet
    if _fernet is None:
        digest = hashlib.sha256(get_settings().secret_key.encode()).digest()
        _fernet = Fernet(base64.urlsafe_b64encode(digest))
    return _fernet


def encrypt_api_key(plain: str) -> str:
    """加密明文 api_key，返回可入库的密文字符串。"""
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_api_key(enc: str) -> str:
    """解密 api_key 密文；密钥轮换或密文损坏时抛 cryptography 异常（调用方降级处理）。"""
    return _get_fernet().decrypt(enc.encode()).decode()


async def get_user_key(session: AsyncSession, user_id: int) -> UserLlmKey | None:
    """查用户自定义 Key 记录（每用户至多一条，user_id UNIQUE）。"""
    return (
        await session.execute(select(UserLlmKey).where(UserLlmKey.user_id == user_id))
    ).scalar_one_or_none()


async def upsert_user_key(
    session: AsyncSession,
    user_id: int,
    base_url: str,
    api_key: str,
    model_short: str | None = None,
    model_medium: str | None = None,
    model_long: str | None = None,
) -> UserLlmKey:
    """新建或覆盖用户自定义 Key：api_key 加密入库并刷新 updated_at；不 commit。"""
    key = await get_user_key(session, user_id)
    if key is None:
        key = UserLlmKey(
            user_id=user_id, base_url=base_url, api_key_enc=encrypt_api_key(api_key)
        )
        session.add(key)
    else:
        key.base_url = base_url
        key.api_key_enc = encrypt_api_key(api_key)
    key.model_short = model_short
    key.model_medium = model_medium
    key.model_long = model_long
    key.updated_at = datetime.now(UTC)
    await session.flush()
    return key


async def delete_user_key(session: AsyncSession, user_id: int) -> bool:
    """删除用户自定义 Key，返回是否存在记录（幂等）；不 commit。"""
    key = await get_user_key(session, user_id)
    if key is None:
        return False
    await session.delete(key)
    await session.flush()
    return True
