"""收藏接口（§12.1，模块 B3，v0.6）：资源 / 帖子收藏，幂等开关语义。

POST 无记录插入（201），已存在幂等返回 200；DELETE 幂等 204。
资源收藏同步维护 resources.fav_count 反规范化计数（同事务）。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.community.social import (
    decide_social_write,
    post_favoritable,
    resource_favoritable,
)
from app.identity.rbac import get_current_user
from app.storage.db import get_db
from app.storage.models import Course, Favorite, Post, Resource, User

router = APIRouter(prefix="/favorites", tags=["favorites"])

LIST_DEFAULT_SIZE = 20
LIST_MAX_SIZE = 100

_TARGET_MODELS = {"resource": Resource, "post": Post}


class FavoriteRequest(BaseModel):
    target_type: Literal["resource", "post"]
    target_id: int


async def _get_fav_target(db: AsyncSession, target_type: str, target_id: int):
    """校验收藏目标存在且可收藏，否则 404（不暴露资源存在性差异）。"""
    target = await db.get(_TARGET_MODELS[target_type], target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Not Found")
    if target_type == "resource" and not resource_favoritable(target.review_status):
        raise HTTPException(status_code=404, detail="Not Found")
    if target_type == "post" and not post_favoritable(target.status):
        raise HTTPException(status_code=404, detail="Not Found")
    return target


async def _find_favorite(
    db: AsyncSession, user_id: int, target_type: str, target_id: int
) -> Favorite | None:
    return (
        await db.execute(
            select(Favorite).where(
                Favorite.user_id == user_id,
                Favorite.target_type == target_type,
                Favorite.target_id == target_id,
            )
        )
    ).scalar_one_or_none()


@router.post("", status_code=201)
@router.post("/", status_code=201)
async def add_favorite(
    payload: FavoriteRequest,
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """收藏资源 / 帖子：重复收藏幂等返回 200（created=false）。"""
    target = await _get_fav_target(db, payload.target_type, payload.target_id)
    existing = await _find_favorite(db, user.id, payload.target_type, payload.target_id)
    created = decide_social_write(existing is not None) == "insert"
    if created:
        db.add(
            Favorite(
                user_id=user.id,
                target_type=payload.target_type,
                target_id=payload.target_id,
            )
        )
        if payload.target_type == "resource":
            target.fav_count += 1
        await db.commit()
    else:
        response.status_code = 200
    return {
        "target_type": payload.target_type,
        "target_id": payload.target_id,
        "favorited": True,
        "created": created,
    }


@router.delete("", status_code=204)
@router.delete("/", status_code=204)
async def remove_favorite(
    payload: FavoriteRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """取消收藏（幂等：无记录也 204）；资源收藏同步递减 fav_count。"""
    existing = await _find_favorite(db, user.id, payload.target_type, payload.target_id)
    if existing is not None:
        if payload.target_type == "resource":
            target = await db.get(Resource, payload.target_id)
            if target is not None and target.fav_count > 0:
                target.fav_count -= 1
        await db.delete(existing)
        await db.commit()


@router.get("")
@router.get("/")
async def list_favorites(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    target_type: Literal["resource", "post"] | None = None,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=LIST_DEFAULT_SIZE, ge=1, le=LIST_MAX_SIZE),
):
    """我的收藏列表（按收藏时间倒序分页），附目标摘要信息。"""
    base = select(Favorite).where(Favorite.user_id == user.id)
    if target_type is not None:
        base = base.where(Favorite.target_type == target_type)
    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            base.order_by(Favorite.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()

    resource_ids = [f.target_id for f in rows if f.target_type == "resource"]
    post_ids = [f.target_id for f in rows if f.target_type == "post"]
    resources: dict[int, tuple[Resource, str]] = {}
    if resource_ids:
        resources = {
            r.id: (r, course_name)
            for r, course_name in (
                await db.execute(
                    select(Resource, Course.name)
                    .join(Course, Course.id == Resource.course_id)
                    .where(Resource.id.in_(resource_ids))
                )
            ).all()
        }
    posts: dict[int, Post] = {}
    if post_ids:
        posts = {
            p.id: p
            for p in (
                await db.execute(select(Post).where(Post.id.in_(post_ids)))
            ).scalars().all()
        }

    items = []
    for f in rows:
        item = {
            "target_type": f.target_type,
            "target_id": f.target_id,
            "created_at": f.created_at,
        }
        if f.target_type == "resource":
            entry = resources.get(f.target_id)
            if entry is None:  # 目标已删除（如下架清理），跳过展示
                continue
            resource, course_name = entry
            item["target"] = {
                "title": resource.title,
                "course_name": course_name,
                "rating": float(resource.rating) if resource.rating is not None else None,
                "download_count": resource.download_count,
            }
        else:
            post = posts.get(f.target_id)
            if post is None:
                continue
            item["target"] = {
                "title": post.title,
                "board": post.board,
                "view_count": post.view_count,
            }
        items.append(item)
    return {"total": total, "items": items}
