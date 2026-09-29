"""公共资源接口（§12.1）：检索、详情与预览、下载（积分校验）、评分。

预览/下载一律由 app 流式转发对象存储字节（不用预签名 URL）；
word/ppt 预览用终审后派生 public 副本的 preview_key（soffice 转换产物）；
下载扣贡献分的上传者分成留 v0.5。
"""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.growth import grant_score
from app.identity.rbac import get_current_user
from app.storage import object_store
from app.storage.db import get_db
from app.storage.models import Document, Resource, ResourceRating, ReviewTask, User

router = APIRouter(prefix="/resources", tags=["resources"])


def _resource_item(resource: Resource, file_type: str, nickname: str) -> dict:
    return {
        "id": resource.id,
        "title": resource.title,
        "description": resource.description,
        "course_id": resource.course_id,
        "chapter_id": resource.chapter_id,
        "file_type": file_type,
        "uploader": {"id": resource.uploader_id, "nickname": nickname},
        "rating": float(resource.rating) if resource.rating is not None else None,
        "rating_count": resource.rating_count,
        "download_count": resource.download_count,
        "fav_count": resource.fav_count,
        "download_cost": resource.download_cost,
        "created_at": resource.created_at,
    }


async def _get_public_copy(db: AsyncSession, doc: Document) -> Document | None:
    """终审通过时复制派生的 public 副本（storage_key 复用原件，凭此定位）。"""
    return (
        await db.execute(
            select(Document).where(
                Document.scope == "public", Document.storage_key == doc.storage_key
            )
        )
    ).scalars().first()


@router.get("", include_in_schema=False)  # 兼容无尾斜杠（nginx 反代下 307 会丢失端口）
@router.get("/")
async def list_resources(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    course_id: int | None = None,
    chapter_id: int | None = None,
    q: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=50)] = 20,
):
    """公共库资源检索：仅已上架，按课程/章节过滤、标题模糊搜索，created_at 倒序分页。"""
    conditions = [Resource.review_status == "approved"]
    if course_id is not None:
        conditions.append(Resource.course_id == course_id)
    if chapter_id is not None:
        conditions.append(Resource.chapter_id == chapter_id)
    if q:
        conditions.append(Resource.title.ilike(f"%{q}%"))
    total = (
        await db.execute(select(func.count()).select_from(Resource).where(*conditions))
    ).scalar_one()
    rows = (
        await db.execute(
            select(Resource, Document.file_type, User.nickname)
            .join(Document, Document.id == Resource.document_id)
            .join(User, User.id == Resource.uploader_id)
            .where(*conditions)
            .order_by(Resource.created_at.desc(), Resource.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()
    return {
        "total": total,
        "items": [_resource_item(r, ft, nickname) for r, ft, nickname in rows],
    }


async def _get_visible_resource(
    db: AsyncSession, resource_id: int, user: User
) -> Resource:
    """资源可见性：approved 任何登录用户 / 本人投稿 / admin，其余 404（§3.2）。"""
    resource = await db.get(Resource, resource_id)
    if resource is None or (
        resource.review_status != "approved"
        and resource.uploader_id != user.id
        and user.role != "admin"
    ):
        raise HTTPException(status_code=404, detail="Not Found")
    return resource


@router.get("/{resource_id}")
async def get_resource(
    resource_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """资源详情：approved / 本人投稿 / admin 可见；附预览可用性与本人评分。"""
    resource = await _get_visible_resource(db, resource_id, user)
    doc = await db.get(Document, resource.document_id)
    uploader = await db.get(User, resource.uploader_id)
    my_rating = (
        await db.execute(
            select(ResourceRating.stars).where(
                ResourceRating.resource_id == resource.id,
                ResourceRating.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    preview_available = doc.file_type in ("pdf_textbook", "markdown")
    if not preview_available:
        public_doc = await _get_public_copy(db, doc)
        preview_available = public_doc is not None and public_doc.preview_key is not None
    return {
        **_resource_item(resource, doc.file_type, uploader.nickname),
        "preview_available": preview_available,
        "my_rating": my_rating,
    }


@router.get("/{resource_id}/preview")
async def preview_resource(
    resource_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """在线预览：inline 流式返回（markdown 原文 / pdf 原文 / word·ppt 转换产物）。

    权限：approved 任何登录用户 / 本人 / admin / 该资源协审或终审阶段的被指派人。
    """
    resource = await db.get(Resource, resource_id)
    if resource is None:
        raise HTTPException(status_code=404, detail="Not Found")
    allowed = (
        resource.review_status == "approved"
        or resource.uploader_id == user.id
        or user.role == "admin"
    )
    if not allowed:
        allowed = (
            await db.execute(
                select(ReviewTask.id)
                .where(
                    ReviewTask.resource_id == resource.id,
                    ReviewTask.stage.in_(["co_review", "final"]),
                    ReviewTask.assignee_ids.any(user.id),
                )
                .limit(1)
            )
        ).first() is not None
    if not allowed:
        raise HTTPException(status_code=404, detail="Not Found")

    doc = await db.get(Document, resource.document_id)
    if doc.file_type in ("markdown", "pdf_textbook"):
        data = await object_store.get_object(doc.storage_key)
        media_type = (
            "text/markdown; charset=utf-8"
            if doc.file_type == "markdown"
            else "application/pdf"
        )
        return Response(
            content=data, media_type=media_type,
            headers={"Content-Disposition": "inline"},
        )
    public_doc = await _get_public_copy(db, doc)
    preview_key = public_doc.preview_key if public_doc is not None else None
    if not preview_key:
        raise HTTPException(status_code=409, detail="预览生成中")
    data = await object_store.get_object(preview_key)
    return Response(
        content=data, media_type="application/pdf",
        headers={"Content-Disposition": "inline"},
    )


@router.get("/{resource_id}/download")
async def download_resource(
    resource_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """下载原件：仅已上架（本人/admin 同）；download_cost>0 时扣贡献分，
    上传者分成留 v0.5（届时按 score_logs ref 结算）。"""
    resource = await db.get(Resource, resource_id)
    if resource is None or resource.review_status != "approved":
        raise HTTPException(status_code=404, detail="Not Found")
    if resource.download_cost > 0:
        if user.score < resource.download_cost:
            raise HTTPException(status_code=422, detail="贡献分不足")
        await grant_score(
            db, user.id, -resource.download_cost, "download_cost", "resource", resource.id
        )
    resource.download_count += 1
    await db.commit()
    doc = await db.get(Document, resource.document_id)
    data = await object_store.get_object(doc.storage_key)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=utf-8''{quote(doc.file_name)}"
        },
    )


class RatingRequest(BaseModel):
    stars: int = Field(ge=1, le=5)


@router.post("/{resource_id}/rating")
async def rate_resource(
    resource_id: int,
    payload: RatingRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """资源评分（1-5 星，一人一评，重复提交覆盖）；不能给自己的资源评分。"""
    resource = await db.get(Resource, resource_id)
    if resource is None or resource.review_status != "approved":
        raise HTTPException(status_code=404, detail="Not Found")
    if resource.uploader_id == user.id:
        raise HTTPException(status_code=422, detail="不能给自己的资源评分")
    existing = (
        await db.execute(
            select(ResourceRating).where(
                ResourceRating.resource_id == resource.id,
                ResourceRating.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            ResourceRating(
                user_id=user.id, resource_id=resource.id, stars=payload.stars
            )
        )
    else:
        existing.stars = payload.stars
    await db.flush()
    avg, count = (
        await db.execute(
            select(func.avg(ResourceRating.stars), func.count()).where(
                ResourceRating.resource_id == resource.id
            )
        )
    ).one()
    resource.rating = round(float(avg), 2)
    resource.rating_count = count
    await db.commit()
    return {
        "rating": float(resource.rating),
        "rating_count": resource.rating_count,
        "my_rating": payload.stars,
    }
