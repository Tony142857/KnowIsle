"""AI 额度与自定义 Key（§10.1）。

扣减顺序：当日免费额度（ai_quotas 按日建行）→ 兑换余额 bonus_balance；
两者皆尽返回 429，提示可用贡献分兑换或配置自定义 Key。
自定义 Key 用户直接放行且不占官方额度（路由层按 provider 判定，跨天一致）。
自定义 Key 用 Fernet 对称加密存储（密钥走 SECRET_KEY 环境变量，见 identity/llm_keys）。

§20.5 B1（v1.1）：consume_quota 合并「检查 + 扣减」为一个原子操作——
对 ai_quotas 当日行 SELECT ... FOR UPDATE 行锁内判定并扣减，并发请求
串行化，不再超扣；路由层的 check_quota 是生成前的只读预检（失败才 429），
预检与扣减之间的并发窗口由原子扣减兜底（耗尽抛 429）。
"""

from datetime import date

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.platform_config import get_config
from app.storage.models import AiQuota, User

_QUOTA_EXHAUSTED_DETAIL = "今日 AI 问答额度已用完，可用贡献分兑换或配置自定义模型 Key"


def can_answer(used: int, daily_limit: int, bonus_balance: int) -> bool:
    """是否还有可用额度：免费额度未用尽，或兑换余额为正。"""
    return used < daily_limit or bonus_balance > 0


def apply_consume(used: int, daily_limit: int, bonus_balance: int) -> tuple[int, int]:
    """按顺序扣减一格（先免费额度，后兑换余额），返回新的 (used, bonus_balance)。"""
    if used < daily_limit:
        return used + 1, bonus_balance
    return used, bonus_balance - 1


async def _get_quota_row(
    session: AsyncSession, user: User, for_update: bool = False
) -> AiQuota:
    """查/建当日额度行（daily_limit 取平台配置 ai_daily_limit，缺省回落 .env
    默认值）；for_update=True 时对已有行加行锁（SELECT ... FOR UPDATE）。
    只 flush，提交由调用方负责。

    并发建行撞 (user_id, date) 唯一约束时走 SAVEPOINT 回退后加锁重读对方
    已建的行，不污染外层事务（qa_logs 落库与额度扣减同事务的语义不变）。
    """
    today = date.today()
    stmt = select(AiQuota).where(AiQuota.user_id == user.id, AiQuota.date == today)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row
    row = AiQuota(
        user_id=user.id,
        date=today,
        used=0,
        daily_limit=await get_config(session, "ai_daily_limit"),
        bonus_balance=0,  # server_default 要 INSERT 后 refresh 才生效，flush 后须显式可读
    )
    session.add(row)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        # 并发请求已建当日行：回退本地插入（SAVEPOINT 回滚后对象回到 pending，
        # 先 expunge 避免重查触发 autoflush 重复 INSERT），再加锁读取对方行
        session.expunge(row)
        row = await session.execute(
            select(AiQuota)
            .where(AiQuota.user_id == user.id, AiQuota.date == today)
            .with_for_update()
        )
        row = row.scalar_one()
    return row


async def ensure_quota(session: AsyncSession, user: User) -> AiQuota:
    """查/建当日额度行（展示/兑换用）；只 flush，提交由调用方负责。"""
    return await _get_quota_row(session, user)


async def check_quota(session: AsyncSession, user: User) -> None:
    """问答前只读预检：免费额度用尽且兑换余额为 0 时抛 429。

    无当日行时只读取平台限额，不插入未提交数据；回答成功后由 consume_quota
    原子建行、复检并扣减，避免 SSE 期间持有唯一键锁。
    """
    quota = (
        await session.execute(
            select(AiQuota).where(AiQuota.user_id == user.id, AiQuota.date == date.today())
        )
    ).scalar_one_or_none()
    if quota is None:
        if not can_answer(0, await get_config(session, "ai_daily_limit"), 0):
            raise HTTPException(status_code=429, detail=_QUOTA_EXHAUSTED_DETAIL)
    elif not can_answer(quota.used, quota.daily_limit, quota.bonus_balance):
        raise HTTPException(status_code=429, detail=_QUOTA_EXHAUSTED_DETAIL)


async def consume_quota(session: AsyncSession, user: User) -> None:
    """问答成功后原子扣减一次（与 qa_logs 同事务提交）。

    §20.5 B1：检查与扣减合并为行锁内的一体操作（SELECT ... FOR UPDATE），
    并发请求串行化、不超扣；锁内发现额度已耗尽（预检后的并发窗口）抛 429。
    """
    quota = await _get_quota_row(session, user, for_update=True)
    if not can_answer(quota.used, quota.daily_limit, quota.bonus_balance):
        raise HTTPException(status_code=429, detail=_QUOTA_EXHAUSTED_DETAIL)
    quota.used, quota.bonus_balance = apply_consume(
        quota.used, quota.daily_limit, quota.bonus_balance
    )
