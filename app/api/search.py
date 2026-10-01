"""全站搜索接口（§12.1，v0.8）：GET /api/search?q=&type=&board=&course_id=&major_id=。

帖子：status ∈ (normal, featured)，ILIKE 匹配标题或正文；资源：仅 approved，
ILIKE 匹配标题或描述。排序「标题命中优先于正文命中，再按时间」（纯 SQL case_when）。
type=all 时两类各取 size 条并各给 total，另一组空返回 {total:0, items:[]}。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.community.search import make_excerpt, validate_query, validate_search_type
from app.identity.rbac import get_current_user
from app.storage.db import get_db
from app.storage.models import Course, Post, Resource, User

router = APIRouter(prefix="/search", tags=["search"])

LIST_MAX_SIZE = 50


@router.get("")
async def search(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str = "",
    search_type: Annotated[str, Query(alias="type")] = "all",
    board: str | None = None,
    course_id: int | None = None,
    major_id: int | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=LIST_MAX_SIZE)] = 20,
):
    """全站搜索（需登录）：帖子与公共资源两路 ILIKE 检索，分组分页返回。"""
    try:
        query = validate_query(q)
        search_type = validate_search_type(search_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    like = f"%{query}%"
    posts_result: dict = {"total": 0, "items": []}
    resources_result: dict = {"total": 0, "items": []}

    if search_type in ("all", "post"):
        title_hit = case((Post.title.ilike(like), 0), else_=1)  # 标题命中优先
        filters = [
            Post.status.in_(["normal", "featured"]),
            or_(Post.title.ilike(like), Post.content.ilike(like)),
        ]
        if board is not None:
            filters.append(Post.board == board)
        if course_id is not None:
            filters.append(Post.course_id == course_id)
        if major_id is not None:
            filters.append(Post.major_id == major_id)
        total = (
            await db.execute(select(func.count()).select_from(Post).where(*filters))
        ).scalar_one()
        rows = (
            await db.execute(
                select(Post, User.nickname)
                .join(User, User.id == Post.author_id)
                .where(*filters)
                .order_by(title_hit, Post.created_at.desc())
                .offset((page - 1) * size)
                .limit(size)
            )
        ).all()
        posts_result = {
            "total": total,
            "items": [
                {
                    "id": post.id,
                    "board": post.board,
                    "title": post.title,
                    "excerpt": make_excerpt(post.content, query),
                    "tags": post.tags or [],
                    "author_nickname": nickname,
                    "course_id": post.course_id,
                    "view_count": post.view_count,
                    "created_at": post.created_at.isoformat() if post.created_at else None,
                }
                for post, nickname in rows
            ],
        }

    if search_type in ("all", "resource"):
        title_hit = case((Resource.title.ilike(like), 0), else_=1)
        filters = [
            Resource.review_status == "approved",
            or_(Resource.title.ilike(like), Resource.description.ilike(like)),
        ]
        if course_id is not None:
            filters.append(Resource.course_id == course_id)
        if major_id is not None:
            filters.append(Course.major_id == major_id)
        base = select(Resource, Course.name).join(Course, Course.id == Resource.course_id)
        total = (
            await db.execute(
                select(func.count())
                .select_from(Resource)
                .join(Course, Course.id == Resource.course_id)
                .where(*filters)
            )
        ).scalar_one()
        rows = (
            await db.execute(
                base.where(*filters)
                .order_by(title_hit, Resource.created_at.desc())
                .offset((page - 1) * size)
                .limit(size)
            )
        ).all()
        resources_result = {
            "total": total,
            "items": [
                {
                    "id": resource.id,
                    "title": resource.title,
                    "description": resource.description,
                    "course_id": resource.course_id,
                    "course_name": course_name,
                    "rating": float(resource.rating) if resource.rating is not None else None,
                    "download_count": resource.download_count,
                    "fav_count": resource.fav_count,
                    "created_at": (
                        resource.created_at.isoformat() if resource.created_at else None
                    ),
                }
                for resource, course_name in rows
            ],
        }

    return {
        "q": query,
        "type": search_type,
        "posts": posts_result,
        "resources": resources_result,
    }
