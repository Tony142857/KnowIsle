"""举报接口（§12.1，v0.8）：资源 / 帖子 / 评论 / 用户举报。

幂等：同一举报人对同一目标已有 open/processing 举报时返回 200 created=false。
举报不自动扣分，扣分走既有管理员信用裁决端点（解耦）；处理流转在管理后台
（GET /api/admin/reports、POST /api/admin/reports/{id}/handle）。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.rbac import get_current_user
from app.storage.db import get_db
from app.storage.models import Comment, Post, Report, Resource, User

router = APIRouter(prefix="/reports", tags=["reports"])


def validate_self_report(target_type: str, target_id: int, reporter_id: int) -> None:
    """自举报校验（纯函数）：举报自己 → ValueError("self_report")。"""
    if target_type == "user" and target_id == reporter_id:
        raise ValueError("self_report")


class CreateReportRequest(BaseModel):
    target_type: Literal["resource", "post", "comment", "user"]
    target_id: int
    reason: str = Field(min_length=1, max_length=500)


async def _check_target(
    db: AsyncSession, target_type: str, target_id: int
) -> None:
    """举报目标可见性校验：不存在或当前状态下不可举报一律 404（不暴露存在性）。

    resource 仅 approved 可举报；post 仅 normal/featured；comment 存在即可。
    user 目标仅 active 可举报（冻结/禁言用户由管理侧处置，不再受理举报）。
    """
    if target_type == "resource":
        target = await db.get(Resource, target_id)
        if target is None or target.review_status != "approved":
            raise HTTPException(status_code=404, detail="Not Found")
    elif target_type == "post":
        target = await db.get(Post, target_id)
        if target is None or target.status not in ("normal", "featured"):
            raise HTTPException(status_code=404, detail="Not Found")
    elif target_type == "comment":
        if await db.get(Comment, target_id) is None:
            raise HTTPException(status_code=404, detail="Not Found")
    else:  # user
        target = await db.get(User, target_id)
        if target is None or target.status != "active":
            raise HTTPException(status_code=404, detail="Not Found")


@router.post("", status_code=201, include_in_schema=False)  # 兼容无尾斜杠
@router.post("/", status_code=201)
async def create_report(
    payload: CreateReportRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """发起举报：目标校验（404/422）→ 幂等查重 → 建档（open）。"""
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="举报理由不能为空")
    try:
        validate_self_report(payload.target_type, payload.target_id, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="不能举报自己") from exc
    await _check_target(db, payload.target_type, payload.target_id)

    existing = (
        await db.execute(
            select(Report).where(
                Report.reporter_id == user.id,
                Report.target_type == payload.target_type,
                Report.target_id == payload.target_id,
                Report.status.in_(["open", "processing"]),
            )
        )
    ).scalars().first()
    if existing is not None:
        return JSONResponse(
            {"report_id": existing.id, "created": False}, status_code=200
        )
    report = Report(
        reporter_id=user.id,
        target_type=payload.target_type,
        target_id=payload.target_id,
        reason=reason,
    )
    db.add(report)
    await db.commit()
    return {"report_id": report.id, "created": True}
