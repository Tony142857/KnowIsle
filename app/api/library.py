"""个人知识库接口（§4.1/§5.1）：学期 → 课程 → 章节树（个人视角），仅本人可见。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.rbac import get_current_user
from app.storage.db import get_db
from app.storage.models import (
    Chapter,
    Course,
    Document,
    Resource,
    ReviewRecord,
    ReviewTask,
    Semester,
    User,
)

router = APIRouter(prefix="/library", tags=["library"])


def _build_tree(chapters: list[Chapter]) -> list[dict]:
    nodes = {
        c.id: {"id": c.id, "title": c.title, "order_idx": c.order_idx, "children": []}
        for c in chapters
    }
    roots = []
    for c in sorted(chapters, key=lambda x: (x.order_idx, x.id)):
        node = nodes[c.id]
        if c.parent_id is not None and c.parent_id in nodes:
            nodes[c.parent_id]["children"].append(node)
        else:
            roots.append(node)
    return roots


async def _get_owned_course(db: AsyncSession, course_id: int, user: User) -> Course:
    """仅本人的个人库课程可访问，越权 404（§3.2）。"""
    course = await db.get(Course, course_id)
    if course is None or course.scope != "personal" or course.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Not Found")
    return course


async def _course_briefs(db: AsyncSession, user: User) -> list[dict]:
    """本人个人课程列表（含文档数与章节树）。"""
    courses = (
        await db.execute(
            select(Course)
            .where(Course.scope == "personal", Course.owner_id == user.id)
            .order_by(Course.id)
        )
    ).scalars().all()
    if not courses:
        return []
    course_ids = [c.id for c in courses]
    doc_counts = dict(
        (
            await db.execute(
                select(Document.course_id, func.count())
                .where(Document.course_id.in_(course_ids))
                .group_by(Document.course_id)
            )
        ).all()
    )
    chapters = (
        await db.execute(select(Chapter).where(Chapter.course_id.in_(course_ids)))
    ).scalars().all()
    chapters_by_course: dict[int, list[Chapter]] = {}
    for ch in chapters:
        chapters_by_course.setdefault(ch.course_id, []).append(ch)
    return [
        {
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "status": c.status,
            "semester_id": c.semester_id,
            "doc_count": doc_counts.get(c.id, 0),
            "chapters": _build_tree(chapters_by_course.get(c.id, [])),
        }
        for c in courses
    ]


@router.get("", include_in_schema=False)  # 兼容无尾斜杠（同上）
@router.get("/")
async def get_library(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """本人个人库树：学期分组 + 未归档课程（semester_id 为空）。"""
    semesters = (
        await db.execute(
            select(Semester).where(Semester.owner_id == user.id).order_by(Semester.id)
        )
    ).scalars().all()
    briefs = await _course_briefs(db, user)
    by_semester: dict[int, list[dict]] = {}
    uncategorized = []
    for brief in briefs:
        if brief["semester_id"] is None:
            uncategorized.append(brief)
        else:
            by_semester.setdefault(brief["semester_id"], []).append(brief)
    return {
        "semesters": [
            {"id": s.id, "name": s.name, "courses": by_semester.get(s.id, [])}
            for s in semesters
        ],
        "uncategorized_courses": uncategorized,
    }


@router.get("/documents")
async def list_library_documents(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """本人文档的解析与最新投稿状态，含可展示的驳回理由和资源链接。"""
    documents = (
        await db.execute(
            select(Document)
            .where(Document.owner_id == user.id, Document.scope == "personal")
            .order_by(Document.created_at.desc(), Document.id.desc())
        )
    ).scalars().all()
    doc_ids = [doc.id for doc in documents]
    resources = []
    if doc_ids:
        resources = (
            await db.execute(
                select(Resource)
                .where(Resource.document_id.in_(doc_ids))
                .order_by(Resource.id.desc())
            )
        ).scalars().all()
    latest_resources = {}
    for resource in resources:
        latest_resources.setdefault(resource.document_id, resource)
    tasks = []
    if latest_resources:
        tasks = (
            await db.execute(
                select(ReviewTask)
                .where(ReviewTask.resource_id.in_([r.id for r in latest_resources.values()]))
                .order_by(ReviewTask.id.desc())
            )
        ).scalars().all()
    latest_tasks = {}
    for task in tasks:
        latest_tasks.setdefault(task.resource_id, task)
    records = []
    if latest_tasks:
        records = (
            await db.execute(
                select(ReviewRecord)
                .where(
                    ReviewRecord.task_id.in_([t.id for t in latest_tasks.values()]),
                    ReviewRecord.verdict == "reject",
                )
                .order_by(ReviewRecord.id.desc())
            )
        ).scalars().all()
    reasons = {}
    task_resources = {task.id: task.resource_id for task in latest_tasks.values()}
    for record in records:
        reasons.setdefault(task_resources[record.task_id], record.comment)
    items = []
    for doc in documents:
        resource = latest_resources.get(doc.id)
        task = latest_tasks.get(resource.id) if resource else None
        reason = reasons.get(resource.id) if resource else None
        if reason is None and task and task.precheck_result:
            reason = task.precheck_result.get("reason") or None
        items.append(
            {
                "document_id": doc.id,
                "file_name": doc.file_name,
                "status": doc.status,
                "parse_error": doc.parse_error if doc.status == "failed" else None,
                "review_status": resource.review_status if resource else None,
                "review_stage": task.stage if task else None,
                "review_reason": reason,
                "resource_id": resource.id if resource else None,
                "resource_url": f"/resources/{resource.id}" if resource else None,
            }
        )
    return {"items": items}


class CreateSemesterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


@router.post("/semesters")
async def create_semester(
    payload: CreateSemesterRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """创建学期；同名幂等返回已有（200）。"""
    existing = (
        await db.execute(
            select(Semester).where(
                Semester.owner_id == user.id, Semester.name == payload.name
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return JSONResponse(
            {"id": existing.id, "name": existing.name}, status_code=200
        )
    semester = Semester(owner_id=user.id, name=payload.name)
    db.add(semester)
    await db.commit()
    return JSONResponse({"id": semester.id, "name": semester.name}, status_code=201)


class CreatePersonalCourseRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    semester_id: int | None = None
    description: str | None = Field(default=None, max_length=512)


@router.post("/courses", status_code=201)
async def create_personal_course(
    payload: CreatePersonalCourseRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """创建个人库课程（scope=personal、owner=本人）。"""
    if payload.semester_id is not None:
        semester = await db.get(Semester, payload.semester_id)
        if semester is None or semester.owner_id != user.id:
            raise HTTPException(status_code=404, detail="Not Found")
    course = Course(
        scope="personal",
        owner_id=user.id,
        semester_id=payload.semester_id,
        name=payload.name,
        description=payload.description,
        status="active",
    )
    db.add(course)
    await db.commit()
    return {
        "id": course.id,
        "name": course.name,
        "description": course.description,
        "status": course.status,
        "semester_id": course.semester_id,
    }


@router.get("/courses/{course_id}/chapters")
async def get_personal_chapters(
    course_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """个人课程章节树（仅本人，越权 404）。"""
    await _get_owned_course(db, course_id, user)
    chapters = (
        await db.execute(select(Chapter).where(Chapter.course_id == course_id))
    ).scalars().all()
    return {"course_id": course_id, "items": _build_tree(chapters)}


class CreatePersonalChapterRequest(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    parent_id: int | None = None
    order_idx: int = 0


@router.post("/courses/{course_id}/chapters", status_code=201)
async def create_personal_chapter(
    course_id: int,
    payload: CreatePersonalChapterRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """个人课程手动建章节/小节（与 tree_builder 自动构建的章节共存，按 title 去重复用）。"""
    await _get_owned_course(db, course_id, user)
    if payload.parent_id is not None:
        parent = await db.get(Chapter, payload.parent_id)
        if parent is None or parent.course_id != course_id:
            raise HTTPException(status_code=422, detail="父章节不存在或不属于本课程")
    chapter = Chapter(
        course_id=course_id,
        title=payload.title,
        parent_id=payload.parent_id,
        order_idx=payload.order_idx,
    )
    db.add(chapter)
    await db.commit()
    return {"id": chapter.id, "title": chapter.title,
            "parent_id": chapter.parent_id, "order_idx": chapter.order_idx}
