"""管理后台接口（§12.1）：空间管理（v0.2 子集）、三级审核终审（v0.4）、审计日志（§3.2）。

所有管理操作写 audit_logs，可追溯、可回滚。
用户治理 / 运营看板在 v0.6 落地。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.rbac import require_admin
from app.moderation.workflow import final_verdict
from app.storage.db import get_db
from app.storage.models import (
    AuditLog,
    Course,
    Document,
    Major,
    Resource,
    ReviewRecord,
    ReviewTask,
    User,
)
from app.workers.pool import get_arq_pool

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

# ---------------------------------------------------------------------------
# 三级审核：任务总览与管理员终审（模块 B2，§12.2）
# ---------------------------------------------------------------------------


@router.get("/review/tasks")
async def list_review_tasks(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    stage: str | None = None,
):
    """审核任务总览：默认列出协审/终审中的全部任务 + 最近 20 条已完成；
    指定 stage 时只列该阶段全部任务。"""

    async def _query(stages: list[str], limit: int | None = None):
        stmt = (
            select(ReviewTask, Resource, Course.name, User.nickname)
            .join(Resource, Resource.id == ReviewTask.resource_id)
            .join(Course, Course.id == Resource.course_id)
            .join(User, User.id == Resource.uploader_id)
            .where(ReviewTask.stage.in_(stages))
            .order_by(ReviewTask.created_at.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await db.execute(stmt)).all()

    if stage is not None:
        rows = await _query([stage])
    else:
        rows = await _query(["co_review", "final"]) + await _query(["done"], 20)
    task_ids = [task.id for task, *_ in rows]
    record_counts: dict[int, int] = {}
    if task_ids:
        record_counts = dict(
            (
                await db.execute(
                    select(ReviewRecord.task_id, func.count())
                    .where(ReviewRecord.task_id.in_(task_ids))
                    .group_by(ReviewRecord.task_id)
                )
            ).all()
        )
    return {
        "items": [
            {
                "task_id": task.id,
                "stage": task.stage,
                "precheck_result": task.precheck_result,
                "assignee_ids": task.assignee_ids,
                "created_at": task.created_at,
                "finished_at": task.finished_at,
                "record_count": record_counts.get(task.id, 0),
                "resource": {
                    "id": resource.id,
                    "title": resource.title,
                    "review_status": resource.review_status,
                    "course_id": resource.course_id,
                    "course_name": course_name,
                    "uploader": {"id": resource.uploader_id, "nickname": nickname},
                },
            }
            for task, resource, course_name, nickname in rows
        ]
    }


class FinalVerdictRequest(BaseModel):
    task_id: int
    verdict: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=512)


@router.post("/review/final")
async def final_review(
    payload: FinalVerdictRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """管理员终审（§12.2）：通过后复制派生 public 副本、结算积分，
    并入队 Office→PDF 预览转换（仅 word/ppt）与目标课程章节摘要回填。"""
    task = await db.get(ReviewTask, payload.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        score_granted, new_doc_id = await final_verdict(
            db, admin, task, payload.verdict, payload.comment
        )
    except ValueError as exc:
        if str(exc) == "comment_required":
            raise HTTPException(status_code=422, detail="驳回必须填写理由") from exc
        raise HTTPException(status_code=404, detail="Not Found") from exc
    await _audit(db, admin.id, "final_verdict",
                 {"task_id": task.id, "resource_id": task.resource_id,
                  "verdict": payload.verdict, "comment": payload.comment})
    await db.commit()
    if payload.verdict == "approve":
        resource = await db.get(Resource, task.resource_id)
        new_doc = await db.get(Document, new_doc_id)
        pool = await get_arq_pool()
        if new_doc.file_type in ("word", "ppt"):
            await pool.enqueue_job("convert_preview", new_doc_id)
        await pool.enqueue_job("backfill_chapter_summaries", resource.course_id)
    return {
        "resource_id": task.resource_id,
        "review_status": "approved" if payload.verdict == "approve" else "rejected",
        "score_granted": score_granted,
    }

# TODO(v0.6): PUT /users/{id}/role（角色任命）、POST /users/{id}/credit（信用裁决）、
#             GET /dashboard（运营看板）
