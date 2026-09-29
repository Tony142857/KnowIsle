"""管理后台接口（§12.1）：空间管理（v0.2 子集）、三级审核终审（v0.4）、审计日志（§3.2）。

所有管理操作写 audit_logs，可追溯、可回滚。
用户治理 / 运营看板在 v0.6 落地。
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.rbac import require_admin
from app.moderation.assign import assign_reviewers
from app.moderation.workflow import direct_verdict, final_verdict
from app.storage.db import get_db
from app.storage.models import (
    AuditLog,
    Course,
    Document,
    Major,
    Notification,
    Resource,
    ReviewRecord,
    ReviewTask,
    User,
)
from app.workers.pool import get_arq_pool
from app.workers.review_timeout_worker import CO_REVIEW_NEEDED, merge_assignees

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


class ReassignRequest(BaseModel):
    task_id: int
    reviewer_ids: list[int] | None = Field(default=None, min_length=1)


@router.post("/review/reassign")
async def reassign_reviewers(
    payload: ReassignRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """管理员改派协审员（v0.5）：指定协审员，或缺省时自动补足到 2 人。

    已提交协审意见者保留；改派后协审计时重置。无可用协审员且无已提交意见时，
    按既定兜底策略直送终审（与超时扫描一致）。
    """
    task = await db.get(ReviewTask, payload.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    if task.stage != "co_review":
        raise HTTPException(status_code=422, detail="任务不在协审阶段，无法改派")
    resource = await db.get(Resource, task.resource_id)
    old_ids = task.assignee_ids or []
    voted_ids = list(
        (
            await db.execute(
                select(ReviewRecord.reviewer_id).where(
                    ReviewRecord.task_id == task.id,
                    ReviewRecord.stage == "co_review",
                )
            )
        ).scalars().all()
    )
    keep = [uid for uid in old_ids if uid in set(voted_ids)]
    if payload.reviewer_ids is not None:
        candidates = (
            await db.execute(select(User).where(User.id.in_(payload.reviewer_ids)))
        ).scalars().all()
        by_id = {u.id: u for u in candidates}
        invalid = [
            rid
            for rid in payload.reviewer_ids
            if (u := by_id.get(rid)) is None
            or u.role != "reviewer"
            or u.status != "active"
            or rid == resource.uploader_id
        ]
        if invalid:
            raise HTTPException(status_code=422, detail=f"协审员不合格：{invalid}")
        conflict = [rid for rid in payload.reviewer_ids if rid in set(voted_ids)]
        if conflict:
            raise HTTPException(
                status_code=422, detail=f"协审员已提交意见，不可重复指派：{conflict}"
            )
        new_ids = list(dict.fromkeys(payload.reviewer_ids))
    else:
        needed = CO_REVIEW_NEEDED - len(keep)
        new_ids = (
            # 排除全部原指派者，避免自动改派落回同一人
            await assign_reviewers(db, resource, count=needed, exclude_ids=old_ids)
            if needed > 0
            else []
        )
    escalated = False
    if not new_ids and not keep:
        # 兜底：无可用协审员，直送管理员终审并通知全体在职管理员
        task.stage = "final"
        escalated = True
        admins = (
            await db.execute(
                select(User).where(User.role == "admin", User.status == "active")
            )
        ).scalars().all()
        for a in admins:
            db.add(
                Notification(
                    user_id=a.id,
                    type="review_escalate",
                    title=f"协审无人可指派，直送终审：{resource.title}",
                    body="管理员改派时无可重新指派的协审员，请管理员终审",
                    link="/admin/review",
                )
            )
        new_assignee_ids: list[int] = []
    else:
        new_assignee_ids = merge_assignees(old_ids, voted_ids, new_ids)
        task.assignee_ids = new_assignee_ids
        task.created_at = datetime.now(UTC)  # 改派后重新计时
        for uid in new_ids:
            db.add(
                Notification(
                    user_id=uid,
                    type="review_assign",
                    title=f"新协审任务：{resource.title}",
                    body="管理员改派协审",
                    link="/review",
                )
            )
    await _audit(db, admin.id, "review_reassign",
                 {"task_id": task.id, "old_assignees": old_ids,
                  "new_assignees": new_assignee_ids})
    await db.commit()
    return {"task_id": task.id, "assignee_ids": new_assignee_ids, "escalated": escalated}


class DirectVerdictRequest(BaseModel):
    task_id: int
    verdict: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=512)


@router.post("/review/direct")
async def direct_review(
    payload: DirectVerdictRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """管理员直审（v0.5）：协审中/待终审任务直接裁决，复用终审路径，
    通过后同样复制派生 public 副本、结算积分并入队预览转换与摘要回填。"""
    task = await db.get(ReviewTask, payload.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not Found")
    from_stage = task.stage
    try:
        score_granted, new_doc_id = await direct_verdict(
            db, admin, task, payload.verdict, payload.comment
        )
    except ValueError as exc:
        if str(exc) == "comment_required":
            raise HTTPException(status_code=422, detail="驳回必须填写理由") from exc
        if str(exc) == "precheck":
            raise HTTPException(status_code=422, detail="预检未完成，无法直审") from exc
        if str(exc) == "done":
            raise HTTPException(status_code=422, detail="任务已完结") from exc
        raise HTTPException(status_code=404, detail="Not Found") from exc
    await _audit(db, admin.id, "review_direct_verdict",
                 {"task_id": task.id, "resource_id": task.resource_id,
                  "verdict": payload.verdict, "comment": payload.comment,
                  "from_stage": from_stage})
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
