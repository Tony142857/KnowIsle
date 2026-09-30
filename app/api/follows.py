"""关注接口（§12.1，模块 B3，v0.6）：关注课程 / 用户，幂等开关语义。

关注课程是订阅类通知（new_resource）的事件源：终审上架到某课程时
通知该课程全部关注者（见 moderation/workflow._approve）。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.community.social import course_followable, decide_social_write, user_followable
from app.identity.rbac import get_current_user
from app.storage.db import get_db
from app.storage.models import Course, Follow, Major, User

router = APIRouter(prefix="/follows", tags=["follows"])

LIST_DEFAULT_SIZE = 20
LIST_MAX_SIZE = 100


class FollowRequest(BaseModel):
    target_type: Literal["course", "user"]
    target_id: int


async def _check_follow_target(
    db: AsyncSession, user: User, target_type: str, target_id: int
) -> None:
    """校验关注目标存在且可关注，否则 404（不暴露资源存在性差异）。"""
    if target_type == "course":
        course = await db.get(Course, target_id)
        if course is None or not course_followable(course.scope, course.status):
            raise HTTPException(status_code=404, detail="Not Found")
    else:
        target = await db.get(User, target_id)
        if target is None or not user_followable(target.status, target.id == user.id):
            raise HTTPException(status_code=404, detail="Not Found")


async def _find_follow(
    db: AsyncSession, user_id: int, target_type: str, target_id: int
) -> Follow | None:
    return (
        await db.execute(
            select(Follow).where(
                Follow.user_id == user_id,
                Follow.target_type == target_type,
                Follow.target_id == target_id,
            )
        )
    ).scalar_one_or_none()


@router.post("", status_code=201)
@router.post("/", status_code=201)
async def add_follow(
    payload: FollowRequest,
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """关注课程 / 用户：重复关注幂等返回 200（created=false）。"""
    await _check_follow_target(db, user, payload.target_type, payload.target_id)
    existing = await _find_follow(db, user.id, payload.target_type, payload.target_id)
    created = decide_social_write(existing is not None) == "insert"
    if created:
        db.add(
            Follow(
                user_id=user.id,
                target_type=payload.target_type,
                target_id=payload.target_id,
            )
        )
        await db.commit()
    else:
        response.status_code = 200
    return {
        "target_type": payload.target_type,
        "target_id": payload.target_id,
        "following": True,
        "created": created,
    }


@router.delete("", status_code=204)
@router.delete("/", status_code=204)
async def remove_follow(
    payload: FollowRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """取消关注（幂等：无记录也 204）。"""
    existing = await _find_follow(db, user.id, payload.target_type, payload.target_id)
    if existing is not None:
        await db.delete(existing)
        await db.commit()


@router.get("/state")
async def follow_state(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    target_type: Literal["course", "user"],
    target_id: int,
):
    """单个目标的关注状态与关注者数（页面关注按钮初始态 / 轮询用）。"""
    following = await _find_follow(db, user.id, target_type, target_id) is not None
    followers = (
        await db.execute(
            select(func.count())
            .select_from(Follow)
            .where(Follow.target_type == target_type, Follow.target_id == target_id)
        )
    ).scalar_one()
    return {"following": following, "followers": followers}


@router.get("")
@router.get("/")
async def list_follows(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    target_type: Literal["course", "user"] | None = None,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=LIST_DEFAULT_SIZE, ge=1, le=LIST_MAX_SIZE),
):
    """我的关注列表（按关注时间倒序分页），附目标摘要信息。"""
    base = select(Follow).where(Follow.user_id == user.id)
    if target_type is not None:
        base = base.where(Follow.target_type == target_type)
    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            base.order_by(Follow.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()

    course_ids = [f.target_id for f in rows if f.target_type == "course"]
    user_ids = [f.target_id for f in rows if f.target_type == "user"]
    courses: dict[int, tuple[Course, str | None]] = {}
    if course_ids:
        courses = {
            c.id: (c, major_name)
            for c, major_name in (
                await db.execute(
                    select(Course, Major.name)
                    .outerjoin(Major, Major.id == Course.major_id)
                    .where(Course.id.in_(course_ids))
                )
            ).all()
        }
    users: dict[int, User] = {}
    if user_ids:
        users = {
            u.id: u
            for u in (
                await db.execute(select(User).where(User.id.in_(user_ids)))
            ).scalars().all()
        }

    items = []
    for f in rows:
        item = {
            "target_type": f.target_type,
            "target_id": f.target_id,
            "created_at": f.created_at,
        }
        if f.target_type == "course":
            entry = courses.get(f.target_id)
            if entry is None:
                continue
            course, major_name = entry
            item["target"] = {
                "name": course.name,
                "major_name": major_name,
                "status": course.status,
            }
        else:
            target = users.get(f.target_id)
            if target is None:
                continue
            item["target"] = {
                "nickname": target.nickname,
                "level": target.level,
                "score": target.score,
            }
        items.append(item)
    return {"total": total, "items": items}
