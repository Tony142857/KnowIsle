"""用户接口（§12.1）：当前用户、公开主页。

隐私要求（§3.1 / §17.3）：学籍信息（真实姓名）仅用于认证核验，不对外暴露；
学号仅本人可见；公开主页只展示昵称 / 头像 / 等级 / 贡献分。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.rbac import get_current_user
from app.storage.db import get_db
from app.storage.models import Major, User

router = APIRouter(prefix="/users", tags=["users"])


def _major_brief(major: Major | None) -> dict | None:
    return {"id": major.id, "name": major.name} if major else None


@router.get("/me")
async def get_me(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """当前用户信息（含学号；真实姓名不下发，前端无需也不应展示）。"""
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

# TODO(v0.5): GET /me/quota（AI 额度查询）；PUT/DELETE /me/llm-key（自定义 Key，P2）
