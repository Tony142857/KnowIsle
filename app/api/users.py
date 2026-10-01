"""用户接口（§12.1）：当前用户、公开主页、AI 额度与自定义 Key。

隐私要求（§3.1 / §17.3）：学籍信息（真实姓名）仅用于认证核验，不对外暴露；
学号仅本人可见；公开主页只展示昵称 / 头像 / 等级 / 贡献分。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.platform_config import get_config
from app.identity.growth import grant_score
from app.identity.llm_keys import delete_user_key, get_user_key, upsert_user_key
from app.identity.quota import ensure_quota
from app.identity.rbac import get_current_user
from app.storage.db import get_db
from app.storage.models import Major, ScoreLog, User

router = APIRouter(prefix="/users", tags=["users"])

LIST_DEFAULT_SIZE = 20
LIST_MAX_SIZE = 100


def _major_brief(major: Major | None) -> dict | None:
    return {"id": major.id, "name": major.name} if major else None


@router.get("/me")
async def get_me(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """当前用户信息（含学号；真实姓名不下发，前端无需也不应展示）。

    v0.8：补充 status / muted_until，供个人中心展示信用处罚状态。
    """
    major = await db.get(Major, user.major_id) if user.major_id else None
    return {
        "id": user.id,
        "student_no": user.student_no,
        "nickname": user.nickname,
        "avatar_url": user.avatar_url,
        "role": user.role,
        "score": user.score,
        "level": user.level,
        "credit": user.credit,
        "status": user.status,
        "muted_until": user.muted_until.isoformat() if user.muted_until else None,
        "grade": user.grade,
        "major": _major_brief(major),
    }


class UpdateMeRequest(BaseModel):
    nickname: str | None = Field(default=None, min_length=2, max_length=32)
    avatar_url: str | None = Field(default=None, max_length=512)


@router.patch("/me")
async def update_me(
    payload: UpdateMeRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """修改昵称 / 头像。昵称全站唯一。"""
    if payload.nickname is not None and payload.nickname != user.nickname:
        taken = (
            await db.execute(select(User.id).where(User.nickname == payload.nickname))
        ).scalar_one_or_none()
        if taken is not None:
            raise HTTPException(status_code=422, detail="昵称已被使用")
        user.nickname = payload.nickname
    if payload.avatar_url is not None:
        user.avatar_url = payload.avatar_url
    await db.commit()
    return {"id": user.id, "nickname": user.nickname, "avatar_url": user.avatar_url}


@router.get("/{user_id}/profile")
async def get_profile(user_id: int, db: Annotated[AsyncSession, Depends(get_db)]):
    """用户公开主页：仅昵称 / 头像 / 等级 / 贡献分 / 专业（§3.3.4 画像授权后续迭代）。"""
    user = await db.get(User, user_id)
    if user is None or user.status == "frozen":
        raise HTTPException(status_code=404, detail="Not Found")
    major = await db.get(Major, user.major_id) if user.major_id else None
    return {
        "id": user.id,
        "nickname": user.nickname,
        "avatar_url": user.avatar_url,
        "role": user.role,
        "level": user.level,
        "score": user.score,
        "major": _major_brief(major),
    }

# ---------------------------------------------------------------------------
# AI 额度与自定义 LLM Key（§10.1，v0.5）
# ---------------------------------------------------------------------------


@router.get("/me/quota")
async def get_my_quota(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """当日 AI 额度查询：免费剩余 + 兑换余额 + 兑换汇率 + 是否已配置自定义 Key。"""
    quota = await ensure_quota(db, user)
    custom_key = await get_user_key(db, user.id) is not None
    return {
        "date": quota.date.isoformat(),
        "used": quota.used,
        "daily_limit": quota.daily_limit,
        "remaining_free": max(quota.daily_limit - quota.used, 0),
        "bonus_balance": quota.bonus_balance,
        "exchange_rate": await get_config(db, "ai_quota_exchange_rate"),
        "custom_key": custom_key,
    }


class QuotaExchangeRequest(BaseModel):
    count: int = Field(ge=1, le=100)  # 兑换次数


@router.post("/me/quota/exchange")
async def exchange_quota(
    payload: QuotaExchangeRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """贡献分兑换 AI 额度：exchange_rate 分 = 1 次（平台配置可调），扣分明细与额度入账同一事务。"""
    cost = await get_config(db, "ai_quota_exchange_rate") * payload.count
    if user.score < cost:
        raise HTTPException(status_code=422, detail="贡献分不足")
    quota = await ensure_quota(db, user)
    await grant_score(db, user.id, -cost, "quota_exchange", "ai_quota", quota.id)
    quota.bonus_balance += payload.count
    await db.commit()
    return {"score": user.score, "bonus_balance": quota.bonus_balance, "spent": cost}


@router.get("/me/llm-key")
async def get_my_llm_key(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """查询自定义 Key 配置状态（api_key 永不回显，仅返回 base_url 与模型名）。"""
    key = await get_user_key(db, user.id)
    if key is None:
        return {"configured": False}
    return {
        "configured": True,
        "base_url": key.base_url,
        "model_short": key.model_short,
        "model_medium": key.model_medium,
        "model_long": key.model_long,
        "updated_at": key.updated_at,
    }


class LlmKeyRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=512)
    api_key: str = Field(min_length=1, max_length=512)
    model_short: str | None = Field(default=None, max_length=128)
    model_medium: str | None = Field(default=None, max_length=128)
    model_long: str | None = Field(default=None, max_length=128)


@router.put("/me/llm-key")
async def put_my_llm_key(
    payload: LlmKeyRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """配置 / 覆盖自定义 Key（每用户一条，upsert；api_key 加密入库）。"""
    base_url = payload.base_url.strip()
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="base_url 须以 http(s):// 开头")
    api_key = payload.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=422, detail="api_key 不能为空")
    await upsert_user_key(
        db,
        user.id,
        base_url,
        api_key,
        model_short=payload.model_short,
        model_medium=payload.model_medium,
        model_long=payload.model_long,
    )
    await db.commit()
    return {"configured": True}


@router.delete("/me/llm-key", status_code=204)
async def delete_my_llm_key(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """删除自定义 Key（幂等：无记录也返回 204）。"""
    await delete_user_key(db, user.id)
    await db.commit()


@router.get("/me/score-logs")
async def get_my_score_logs(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    size: int = Query(default=LIST_DEFAULT_SIZE, ge=1, le=LIST_MAX_SIZE),
):
    """本人贡献分明细（个人中心成长看板，按时间倒序分页）。"""
    stmt = select(ScoreLog).where(ScoreLog.user_id == user.id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(ScoreLog.created_at.desc()).offset((page - 1) * size).limit(size)
        )
    ).scalars().all()
    return {
        "total": total,
        "items": [
            {
                "delta": r.delta,
                "reason": r.reason,
                "ref_type": r.ref_type,
                "ref_id": r.ref_id,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }
