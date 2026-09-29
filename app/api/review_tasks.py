"""协审工作台接口（§12.1）：待审任务队列、任务详情、提交协审意见（协审员角色）。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.rbac import require_reviewer
from app.moderation.workflow import submit_co_verdict
from app.storage.db import get_db
from app.storage.models import (
    Course,
    Document,
    Resource,
    ReviewRecord,
    ReviewTask,
    User,
)

router = APIRouter(prefix="/review", tags=["review-tasks"])


@router.get("/tasks")
async def list_co_tasks(
    user: Annotated[User, Depends(require_reviewer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """本人被指派的协审任务队列（stage=co_review，按创建时间升序，先到期先审）。"""
    rows = (
        await db.execute(
            select(ReviewTask, Resource, Course.name)
            .join(Resource, Resource.id == ReviewTask.resource_id)
            .join(Course, Course.id == Resource.course_id)
            .where(
                ReviewTask.stage == "co_review",
                ReviewTask.assignee_ids.any(user.id),
            )
            .order_by(ReviewTask.created_at)
        )
    ).all()
    return {
        "items": [
            {
                "task_id": task.id,
                "stage": task.stage,
                "precheck_result": task.precheck_result,
                "created_at": task.created_at,
                "resource": {
                    "id": resource.id,
                    "title": resource.title,
                    "course_id": resource.course_id,
                    "course_name": course_name,
                },
            }
            for task, resource, course_name in rows
        ]
    }


async def _get_review_task(db: AsyncSession, task_id: int, user: User) -> ReviewTask:
    """任务可见性：被指派的协审员或 admin，其余 404（§3.2）。"""
    task = await db.get(ReviewTask, task_id)
    if task is None or (user.role != "admin" and user.id not in (task.assignee_ids or [])):
        raise HTTPException(status_code=404, detail="Not Found")
    return task


@router.get("/tasks/{task_id}")
async def get_co_task(
    task_id: int,
    user: Annotated[User, Depends(require_reviewer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """协审任务详情：预检结果 + 已有意见记录 + 资源/文档信息 + 预览地址。"""
    task = await _get_review_task(db, task_id, user)
    resource = await db.get(Resource, task.resource_id)
    doc = await db.get(Document, resource.document_id)
    course = await db.get(Course, resource.course_id)
    records = (
        await db.execute(
            select(ReviewRecord)
            .where(ReviewRecord.task_id == task.id)
            .order_by(ReviewRecord.id)
        )
    ).scalars().all()
    return {
        "task_id": task.id,
        "stage": task.stage,
        "precheck_result": task.precheck_result,
        "assignee_ids": task.assignee_ids,
        "created_at": task.created_at,
        "finished_at": task.finished_at,
        "records": [
            {
                "reviewer_id": r.reviewer_id,
                "stage": r.stage,
                "verdict": r.verdict,
                "comment": r.comment,
                "created_at": r.created_at,
            }
            for r in records
        ],
        "resource": {
            "id": resource.id,
            "title": resource.title,
            "description": resource.description,
            "course_id": resource.course_id,
            "course_name": course.name,
            "chapter_id": resource.chapter_id,
            "review_status": resource.review_status,
            "created_at": resource.created_at,
        },
        "document": {"file_name": doc.file_name, "file_type": doc.file_type},
        "preview_url": f"/api/resources/{resource.id}/preview",
    }


class VerdictRequest(BaseModel):
    verdict: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=512)


@router.post("/tasks/{task_id}/verdict")
async def submit_verdict(
    task_id: int,
    payload: VerdictRequest,
    user: Annotated[User, Depends(require_reviewer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """提交协审意见：人数未齐 recorded；多数决通过 advanced_final；多数驳回 rejected。"""
    task = await db.get(ReviewTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        status = await submit_co_verdict(db, user, task, payload.verdict, payload.comment)
    except ValueError as exc:
        if str(exc) == "already":
            raise HTTPException(status_code=422, detail="已提交过意见") from exc
        if str(exc) == "comment_required":
            raise HTTPException(status_code=422, detail="驳回必须填写理由") from exc
        raise HTTPException(status_code=404, detail="Not Found") from exc
    await db.commit()
    return {"status": status}
