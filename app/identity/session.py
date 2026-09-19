"""会话与令牌双通道（§7.4）：Redis Session（页面）+ JWT（API / 桌面客户端）。

- 页面通道：登录成功签发随机会话令牌写入 HttpOnly Cookie，
  Redis 存 `session:{token} -> user_id`，滑动续期。
- API 通道：`POST /api/auth/token` 凭有效会话签发 JWT（HS256），
  客户端以 `Authorization: Bearer <jwt>` 访问 API。
"""

import secrets
from datetime import UTC, datetime, timedelta

import jwt

from app.config import get_settings

SESSION_KEY_PREFIX = "session:"


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_key(token: str) -> str:
    return f"{SESSION_KEY_PREFIX}{token}"


async def create_session(redis, user_id: int) -> str:
    """创建会话并返回令牌；TTL 取配置，滑动续期在 resolve 时进行。"""
    settings = get_settings()
    token = new_session_token()
    await redis.set(session_key(token), str(user_id), ex=settings.session_ttl_seconds)
    return token


async def resolve_session(redis, token: str) -> int | None:
    """按令牌解析 user_id；命中时滑动续期。无效/过期返回 None。"""
    if not token:
        return None
    settings = get_settings()
    key = session_key(token)
    value = await redis.get(key)
    if value is None:
        return None
    await redis.expire(key, settings.session_ttl_seconds)
    return int(value)


async def destroy_session(redis, token: str) -> None:
    if token:
        await redis.delete(session_key(token))


def issue_jwt(user_id: int) -> str:
    """签发 API 通道 JWT（HS256，sub=user_id）。"""
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(seconds=settings.jwt_ttl_seconds),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def verify_jwt(token: str) -> int | None:
    """校验 JWT 并返回 user_id；签名错误/过期/格式非法返回 None。"""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
