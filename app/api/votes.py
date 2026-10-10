"""投票接口（§12.1）：点赞 / 有用。

§20.5 B4（v1.1）目标状态校验：closed 帖 422；资源仅 approved 可投（其余 404，
与收藏口径一致）；评论投票随所属帖状态（帖子 closed 时评论投票也 422）。
§20.5 B1：首次投票并发撞 (user_id, target_type, target_id) 唯一约束时收口为
幂等语义——rollback 后重查当前状态返回，不出 500。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
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


async def _check_target_votable(db: AsyncSession, target_type: str, target) -> None:
    """目标状态校验（§20.5 B4）：closed 帖 422；未上架资源 404（同收藏口径）；
    评论随所属帖——帖子 closed 时评论投票也 422。"""
    if target_type == "post" and target.status == "closed":
        raise HTTPException(status_code=422, detail="帖子已关闭，不能投票")
    if target_type == "resource" and target.review_status != "approved":
        raise HTTPException(status_code=404, detail="Not Found")
    if target_type == "comment":
        post = await db.get(Post, target.post_id)
        if post is not None and post.status == "closed":
            raise HTTPException(status_code=422, detail="帖子已关闭，不能投票")


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
    await _check_target_votable(db, payload.target_type, target)

    async def _current_state() -> dict:
        """重查我的投票与目标总分（正常路径提交后 / 并发冲突回落共用）。"""
        mine = (
            await db.execute(
                select(Vote.value).where(
                    Vote.user_id == user.id,
                    Vote.target_type == payload.target_type,
                    Vote.target_id == payload.target_id,
                )
            )
        ).scalar_one_or_none()
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
            "my_vote": mine if mine is not None else 0,
            "score": score,
        }

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
    try:
        await db.commit()
    except IntegrityError:
        # §20.5 B1：并发首次投票撞唯一约束 → rollback 后按幂等语义重查当前状态返回
        await db.rollback()
        return await _current_state()
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
