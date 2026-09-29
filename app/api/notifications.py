"""站内通知接口（§12.1）：通知列表、标记已读、未读角标。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.rbac import get_current_user, get_current_user_optional
from app.storage.db import get_db
from app.storage.models import Notification, User

router = APIRouter(prefix="/notifications", tags=["notifications"])

LIST_DEFAULT_SIZE = 20
LIST_MAX_SIZE = 100


@router.get("/unread-count")
async def unread_count(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> dict:
    """导航栏未读角标轮询（HTMX 每 30s）；未登录返回 0。"""
    if user is None:
        return {"unread": 0}
    unread = (
        await db.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user.id, Notification.is_read.is_(False))
        )
    ).scalar_one()
    return {"unread": unread}


@router.get("")
@router.get("/")
async def list_notifications(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    size: int = Query(default=LIST_DEFAULT_SIZE, ge=1, le=LIST_MAX_SIZE),
):
    """通知列表：仅本人，按时间倒序。"""
    base = select(Notification).where(Notification.user_id == user.id)
    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            base.order_by(Notification.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id": n.id,
                "type": n.type,
                "title": n.title,
                "body": n.body,
                "link": n.link,
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in rows
        ],
    }


class MarkReadRequest(BaseModel):
    ids: list[int] | None = None  # 不传表示全部已读


@router.post("/read")
async def mark_read(
    payload: MarkReadRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """标记已读：传 ids 标指定通知（仅限本人），不传全部已读。"""
    stmt = (
        update(Notification)
        .where(Notification.user_id == user.id, Notification.is_read.is_(False))
        .values(is_read=True)
    )
    if payload.ids:
        stmt = stmt.where(Notification.id.in_(payload.ids))
    await db.execute(stmt)
    await db.commit()
    return {"ok": True}
