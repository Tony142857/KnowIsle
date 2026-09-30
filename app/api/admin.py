"""管理后台接口（§12.1）：空间管理（v0.2 子集 + v0.6 课程总览）、三级审核终审（v0.4）、
改派/直审（v0.5）、用户治理与平台配置（v0.6）、审计日志（§3.2）。

所有管理操作写 audit_logs，可追溯、可回滚。
运营看板在 v0.8 落地。
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.platform_config import config_specs, set_config
from app.identity.growth import apply_credit_change
from app.identity.rbac import require_admin
from app.moderation.assign import assign_reviewers
from app.moderation.workflow import direct_verdict, final_verdict
from app.storage.db import get_db
from app.storage.models import (
    AuditLog,
    Course,
    CreditLog,
    Document,
    Major,
    Notification,
    PlatformConfig,
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

# ---------------------------------------------------------------------------
# 用户治理（模块 B6，v0.6）：用户列表 / 角色任命 / 信用裁决
# ---------------------------------------------------------------------------


@router.get("/users")
async def list_users(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = None,
    page: int = 1,
    size: int = 20,
):
    """用户列表（管理后台用户治理）：学号/昵称模糊搜索 + 分页。

    真实姓名（real_name）不下发——仅认证核验用，管理员后台亦不展示（§3.1 隐私）。
    """
    page = max(page, 1)
    size = min(max(size, 1), 100)
    base = select(User)
    if q:
        like = f"%{q}%"
        base = base.where(or_(User.student_no.ilike(like), User.nickname.ilike(like)))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await db.execute(
            base.order_by(User.id).offset((page - 1) * size).limit(size)
        )
    ).scalars().all()
    major_ids = {u.major_id for u in rows if u.major_id is not None}
    majors: dict[int, str] = {}
    if major_ids:
        majors = {
            m.id: m.name
            for m in (
                await db.execute(select(Major).where(Major.id.in_(major_ids)))
            ).scalars().all()
        }
    return {
        "total": total,
        "items": [
            {
                "id": u.id,
                "student_no": u.student_no,
                "nickname": u.nickname,
                "role": u.role,
                "level": u.level,
                "score": u.score,
                "credit": u.credit,
                "status": u.status,
                "major_name": majors.get(u.major_id),
                "created_at": u.created_at,
            }
            for u in rows
        ],
    }


class UpdateRoleRequest(BaseModel):
    role: Literal["student", "reviewer", "builder", "admin"]


@router.put("/users/{user_id}/role")
async def update_user_role(
    user_id: int,
    payload: UpdateRoleRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """角色任命（§3.2 四角色）：审计留痕 + 通知本人；不能修改自己的角色（防自降权锁死）。"""
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Not Found")
    if target.id == admin.id:
        raise HTTPException(status_code=422, detail="不能修改自己的角色")
    old_role = target.role
    if old_role == payload.role:
        return {"id": target.id, "role": target.role, "changed": False}
    target.role = payload.role
    db.add(
        Notification(
            user_id=target.id,
            type="role_change",
            title=f"你的角色已调整为 {payload.role}",
            body=f"原角色 {old_role}，由管理员调整",
            link="/me",
        )
    )
    await _audit(db, admin.id, "role_change",
                 {"user_id": user_id, "from": old_role, "to": payload.role})
    await db.commit()
    return {"id": target.id, "role": target.role, "changed": True}


class CreditAdjustRequest(BaseModel):
    delta: int = Field(ge=-100, le=100)  # 负扣分 / 正恢复；0 由端点拒绝
    reason: str = Field(min_length=1, max_length=200)
    ref_type: str | None = Field(default=None, max_length=32)
    ref_id: int | None = None


@router.post("/users/{user_id}/credit")
async def adjust_credit(
    user_id: int,
    payload: CreditAdjustRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """信用裁决（§3.3.3）：credit_logs 留痕（裁决人+理由）+ 限幅 0~100 + 通知本人。

    阶梯处罚（<80 限流 / <60 禁言 / <40 冻结）的自动执行留 v0.8 与举报一起做。
    """
    if payload.delta == 0:
        raise HTTPException(status_code=422, detail="delta 不能为 0")
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Not Found")
    new_credit = await apply_credit_change(
        db, target, admin.id, payload.delta, payload.reason,
        ref_type=payload.ref_type, ref_id=payload.ref_id,
    )
    await _audit(db, admin.id, "credit_penalty",
                 {"user_id": user_id, "delta": payload.delta,
                  "reason": payload.reason, "credit": new_credit})
    await db.commit()
    return {"id": target.id, "credit": new_credit, "delta": payload.delta}


@router.get("/users/{user_id}/credit-logs")
async def list_credit_logs(
    user_id: int,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """指定用户信用分明细（最近 50 条，含裁决人昵称）。"""
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Not Found")
    rows = (
        await db.execute(
            select(CreditLog, User.nickname)
            .outerjoin(User, User.id == CreditLog.admin_id)
            .where(CreditLog.user_id == user_id)
            .order_by(CreditLog.created_at.desc())
            .limit(50)
        )
    ).all()
    return {
        "items": [
            {
                "delta": log.delta,
                "reason": log.reason,
                "ref_type": log.ref_type,
                "ref_id": log.ref_id,
                "admin_nickname": nickname,
                "created_at": log.created_at,
            }
            for log, nickname in rows
        ]
    }


# ---------------------------------------------------------------------------
# 空间管理（模块 B6，v0.6）：公共课程总览（创建/审批走既有 POST /api/courses
# 与 PATCH /admin/courses/{id}）
# ---------------------------------------------------------------------------


@router.get("/courses")
async def list_admin_courses(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status: Literal["active", "pending", "disabled"] | None = None,
    major_id: int | None = None,
    page: int = 1,
    size: int = 50,
):
    """公共课程空间总览（管理后台空间管理页）：状态/专业过滤 + 分页。"""
    page = max(page, 1)
    size = min(max(size, 1), 100)
    filters = [Course.scope == "public"]
    if status is not None:
        filters.append(Course.status == status)
    if major_id is not None:
        filters.append(Course.major_id == major_id)
    base = (
        select(Course, Major.name)
        .outerjoin(Major, Major.id == Course.major_id)
        .where(*filters)
    )
    total = (
        await db.execute(
            select(func.count()).select_from(Course).where(*filters)
        )
    ).scalar_one()
    rows = (
        await db.execute(
            base.order_by(Course.id).offset((page - 1) * size).limit(size)
        )
    ).all()
    return {
        "total": total,
        "items": [
            {
                "id": c.id,
                "name": c.name,
                "status": c.status,
                "major_id": c.major_id,
                "major_name": major_name,
                "description": c.description,
                "created_at": c.created_at,
            }
            for c, major_name in rows
        ],
    }


# ---------------------------------------------------------------------------
# 平台配置（模块 B6，v0.6）：仅 CONFIG_SPECS 注册键可调，修改写审计
# ---------------------------------------------------------------------------


@router.get("/config")
async def list_platform_config(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """平台配置总览：生效值（DB 覆盖优先）+ .env 默认值 + 最近修改人/时间。"""
    specs = config_specs()
    rows = {
        r.key: r
        for r in (await db.execute(select(PlatformConfig))).scalars().all()
    }
    updater_ids = {r.updated_by for r in rows.values() if r.updated_by is not None}
    updaters: dict[int, str] = {}
    if updater_ids:
        updaters = {
            u.id: u.nickname
            for u in (
                await db.execute(select(User).where(User.id.in_(updater_ids)))
            ).scalars().all()
        }
    return {
        "items": [
            {
                "key": spec.key,
                "value": int(rows[spec.key].value) if spec.key in rows else spec.default,
                "default": spec.default,
                "description": spec.description,
                "overridden": spec.key in rows,
                "min_value": spec.min_value,
                "max_value": spec.max_value,
                "updated_by": (
                    updaters.get(rows[spec.key].updated_by) if spec.key in rows else None
                ),
                "updated_at": rows[spec.key].updated_at if spec.key in rows else None,
            }
            for spec in specs.values()
        ]
    }


class ConfigUpdateRequest(BaseModel):
    value: int


@router.put("/config/{key}")
async def update_platform_config(
    key: str,
    payload: ConfigUpdateRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """调整平台配置：未知键 404，越界 422；修改写 audit_logs（config_change）。"""
    try:
        old_value, new_value = await set_config(db, key, payload.value, admin.id)
    except ValueError as exc:
        if str(exc) == "unknown_key":
            raise HTTPException(status_code=404, detail="Not Found") from exc
        raise HTTPException(status_code=422, detail=f"配置值非法：{exc}") from exc
    await _audit(db, admin.id, "config_change",
                 {"key": key, "from": old_value, "to": new_value})
    await db.commit()
    return {"key": key, "value": new_value, "previous": old_value}

# TODO(v0.8): GET /dashboard（运营看板，模块 A5 指标可视化）
