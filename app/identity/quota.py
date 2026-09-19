"""AI 额度与自定义 Key（§10.1）。

扣减顺序：当日免费额度（ai_quotas 按日建行）→ 兑换余额 bonus_balance；
自定义 Key 用户直接放行（由 user_llm_keys 是否存在记录判定，跨天一致）。
自定义 Key 用 Fernet 对称加密存储（密钥走 SECRET_KEY 环境变量）。

v0.3 简单版：仅实现当日免费额度的检查与扣减。
TODO(v0.5)：自定义 Key 放行、bonus_balance 贡献分兑换（ai_quota_exchange_rate）。
"""

from datetime import date

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.storage.models import AiQuota, User


async def _get_or_create(session: AsyncSession, user: User) -> AiQuota:
    """查/建当日额度行（daily_limit 取配置覆盖默认值）；只 flush，提交由调用方负责。"""
    today = date.today()
    row = (
        await session.execute(
            select(AiQuota).where(AiQuota.user_id == user.id, AiQuota.date == today)
        )
    ).scalar_one_or_none()
    if row is None:
        row = AiQuota(
            user_id=user.id,
            date=today,
            used=0,
            daily_limit=get_settings().ai_daily_free_quota,
        )
        session.add(row)
        await session.flush()
    return row


async def check_quota(session: AsyncSession, user: User) -> None:
    """问答前检查：当日用量达上限抛 429。"""
    quota = await _get_or_create(session, user)
    if quota.used >= quota.daily_limit:
        raise HTTPException(status_code=429, detail="今日 AI 问答额度已用完")


async def consume_quota(session: AsyncSession, user: User) -> None:
    """问答成功后扣减一次（与 qa_logs 同事务提交）。"""
    quota = await _get_or_create(session, user)
    quota.used += 1
