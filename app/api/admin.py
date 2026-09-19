"""管理后台接口（§12.1）：空间管理（v0.2 子集）、审计日志（§3.2）。

所有管理操作写 audit_logs，可追溯、可回滚。
终审 / 用户治理 / 运营看板在 v0.4 / v0.6 落地。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.rbac import require_admin
from app.storage.db import get_db
from app.storage.models import AuditLog, Course, Major, User

router = APIRouter(prefix="/admin", tags=["admin"])


async def _audit(db: AsyncSession, admin_id: int, action: str, detail: dict) -> None:
    db.add(AuditLog(admin_id=admin_id, action=action, detail=detail))


# ---------------------------------------------------------------------------
# 空间管理：专业名单导入（§4.1 / 模块 B6）
# ---------------------------------------------------------------------------


class MajorItem(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    code: str | None = Field(default=None, max_length=32)


class ImportMajorsRequest(BaseModel):
    items: list[MajorItem] = Field(min_length=1)


@router.post("/majors", status_code=201)
async def import_majors(
    payload: ImportMajorsRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """批量导入专业名单（幂等：按 name 去重，已存在的跳过）。"""
    existing = set(
        (await db.execute(select(Major.name))).scalars().all()
    )
    created = []
    for item in payload.items:
        if item.name in existing:
            continue
        major = Major(name=item.name, code=item.code)
        db.add(major)
        existing.add(item.name)
        created.append(item.name)
    await _audit(db, admin.id, "import_majors",
                 {"created": len(created), "skipped": len(payload.items) - len(created)})
    await db.commit()
    return {"created": len(created), "skipped": len(payload.items) - len(created),
            "names": created}


# ---------------------------------------------------------------------------
# 空间管理：课程审批 / 停用（§4.1：学生申请审批开通）
# ---------------------------------------------------------------------------


class UpdateCourseRequest(BaseModel):
    status: str = Field(pattern="^(active|disabled)$")
    description: str | None = Field(default=None, max_length=512)


@router.patch("/courses/{course_id}")
async def update_course(
    course_id: int,
    payload: UpdateCourseRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """课程空间审批（pending → active）或停用（→ disabled），全程审计留痕。"""
    course = await db.get(Course, course_id)
    if course is None or course.scope != "public":
        raise HTTPException(status_code=404, detail="Not Found")
    old_status = course.status
    course.status = payload.status
    if payload.description is not None:
        course.description = payload.description
    await _audit(db, admin.id, "course_status",
                 {"course_id": course_id, "from": old_status, "to": payload.status})
    await db.commit()
    return {"id": course.id, "name": course.name, "status": course.status}

# TODO(v0.4): POST /review/final（管理员终审）
# TODO(v0.6): PUT /users/{id}/role（角色任命）、POST /users/{id}/credit（信用裁决）、
#             GET /dashboard（运营看板）
