"""投票接口（§12.1）：点赞 / 有用。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.community.votes import decide_vote
from app.identity.rbac import get_current_user
from app.storage.db import get_db
from app.storage.models import Comment, Post, Resource, User, Vote

router = APIRouter(prefix="/votes", tags=["votes"])

_TARGET_MODELS = {"post": Post, "comment": Comment, "resource": Resource}


class VoteRequest(BaseModel):
    target_type: Literal["post", "comment", "resource"]
    target_id: int
    value: Literal[1, -1]


@router.post("")
@router.post("/")
async def cast_vote(
    payload: VoteRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """投票开关：无记录插入；同值取消；异值改值。响应目标最新总分与我的投票。"""
    model = _TARGET_MODELS[payload.target_type]
    target = await db.get(model, payload.target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Not Found")

    existing = (
        await db.execute(
            select(Vote).where(
                Vote.user_id == user.id,
                Vote.target_type == payload.target_type,
                Vote.target_id == payload.target_id,
            )
        )
    ).scalar_one_or_none()
    action, my_vote = decide_vote(existing.value if existing else None, payload.value)
    if action == "insert":
        db.add(
            Vote(
                user_id=user.id,
                target_type=payload.target_type,
                target_id=payload.target_id,
                value=payload.value,
            )
        )
    elif action == "delete":
        await db.delete(existing)
    else:
        existing.value = payload.value
    await db.commit()

    score = (
        await db.execute(
            select(func.coalesce(func.sum(Vote.value), 0)).where(
                Vote.target_type == payload.target_type,
                Vote.target_id == payload.target_id,
            )
        )
    ).scalar_one()
    return {
        "target_type": payload.target_type,
        "target_id": payload.target_id,
        "my_vote": my_vote,
        "score": score,
    }
