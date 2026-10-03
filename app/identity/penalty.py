"""信用阶梯处罚门控（v0.8，§3.3.3）：禁言拦截与低信用操作冷却。

- assert_can_speak：发帖/评论入口的禁言检查；muted 到期惰性解除（属性落库，
  由端点既有 commit 一并提交）。frozen 已在 rbac 登录态拦截（401），此处不重复处理。
- check_action_cooldown：credit < TIER_RATE_LIMIT 时按行为维度做 Redis 冷却
  （SET NX EX），超限 429 并提示剩余秒数；Redis 故障仅记日志放行（容错惯例同
  growth.py 榜单写入）。
"""

import logging
from datetime import datetime

from fastapi import HTTPException

from app.identity.growth import TIER_RATE_LIMIT, effective_status
from app.storage.cache import get_redis
from app.storage.models import User

logger = logging.getLogger(__name__)

COOLDOWN_KEY_PREFIX = "knowisle:rl:"


def cooldown_key(action: str, user_id: int) -> str:
    """低信用操作冷却 Redis 键：knowisle:rl:{action}:{user_id}。"""
    return f"{COOLDOWN_KEY_PREFIX}{action}:{user_id}"


def mute_detail(muted_until: datetime | None) -> str:
    """禁言提示文案（纯函数）：含截止时间（ISO），无期限时给通用文案。"""
    if muted_until is not None:
        return f"账号处于禁言期，至 {muted_until.isoformat()} 解除，期间无法发帖与评论"
    return "账号处于禁言期，无法发帖与评论"


def assert_can_speak(user: User, now: datetime) -> None:
    """发言门控：有效状态为 muted → 403（detail 含禁言截止时间）。

    muted 已到期则惰性解除：status 置 active、muted_until 置空（调用方 commit）。
    """
    if user.status != "muted":
        return
    if effective_status(user.status, user.muted_until, now) == "muted":
        raise HTTPException(status_code=403, detail=mute_detail(user.muted_until))
    user.status = "active"
    user.muted_until = None


async def check_action_cooldown(user: User, action: str, cooldown_seconds: int) -> None:
    """低信用操作冷却：credit < 80 时同一行为 SET NX EX 冷却，超限 429（detail 含剩余秒数）。

    credit >= 80 直接放行；Redis 故障仅记日志放行，不阻塞主流程。
    """
    if user.credit >= TIER_RATE_LIMIT:
        return
    try:
        redis = get_redis()
        key = cooldown_key(action, user.id)
        acquired = await redis.set(key, "1", nx=True, ex=cooldown_seconds)
        if not acquired:
            pttl = await redis.pttl(key)
            remaining = max(1, (pttl + 999) // 1000) if pttl > 0 else cooldown_seconds
            raise HTTPException(
                status_code=429,
                detail=f"信用分低于 {TIER_RATE_LIMIT}，操作过于频繁，请 {remaining} 秒后重试",
            )
    except HTTPException:
        raise
    except Exception:
        logger.warning(
            "信用冷却检查失败（放行）user_id=%s action=%s", user.id, action, exc_info=True
        )
