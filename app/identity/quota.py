"""AI 额度与自定义 Key（§10.1）。

扣减顺序：当日免费额度（ai_quotas 按日建行）→ 兑换余额 bonus_balance；
两者皆尽返回 429，提示可用贡献分兑换或配置自定义 Key。
自定义 Key 用户直接放行且不占官方额度（路由层按 provider 判定，跨天一致）。
自定义 Key 用 Fernet 对称加密存储（密钥走 SECRET_KEY 环境变量，见 identity/llm_keys）。
"""

from datetime import date

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.platform_config import get_config
from app.storage.models import AiQuota, User


def can_answer(used: int, daily_limit: int, bonus_balance: int) -> bool:
    """是否还有可用额度：免费额度未用尽，或兑换余额为正。"""
    return used < daily_limit or bonus_balance > 0


def apply_consume(used: int, daily_limit: int, bonus_balance: int) -> tuple[int, int]:
    """按顺序扣减一格（先免费额度，后兑换余额），返回新的 (used, bonus_balance)。"""
    if used < daily_limit:
        return used + 1, bonus_balance
    return used, bonus_balance - 1


async def ensure_quota(session: AsyncSession, user: User) -> AiQuota:
    """查/建当日额度行（daily_limit 取平台配置 ai_daily_limit，缺省回落 .env
    默认值）；只 flush，提交由调用方负责。"""
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
            daily_limit=await get_config(session, "ai_daily_limit"),
            bonus_balance=0,  # server_default 要 INSERT 后 refresh 才生效，flush 后须显式可读
        )
        session.add(row)
        await session.flush()
    return row


async def check_quota(session: AsyncSession, user: User) -> None:
    """问答前检查：免费额度用尽且兑换余额为 0 时抛 429。"""
    quota = await ensure_quota(session, user)
    if not can_answer(quota.used, quota.daily_limit, quota.bonus_balance):
        raise HTTPException(
            status_code=429,
            detail="今日 AI 问答额度已用完，可用贡献分兑换或配置自定义模型 Key",
        )


async def consume_quota(session: AsyncSession, user: User) -> None:
    """问答成功后按顺序扣减一次（与 qa_logs 同事务提交）。"""
    quota = await ensure_quota(session, user)
    quota.used, quota.bonus_balance = apply_consume(
        quota.used, quota.daily_limit, quota.bonus_balance
    )
