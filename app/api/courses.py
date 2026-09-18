"""空间接口（§12.1）：专业列表、课程空间、章节知识树。

空间结构（§4.1）：学院（站点级配置）→ 专业 → 课程空间 → 章节知识树。
- 课程空间：管理员创建直接开通；学生申请为 pending，管理员审批后开通。
- 章节树：builder/admin 人工维护（§3.2）；随资料入库的半自动构建在 v0.3 落地。
- 数据级权限：pending 课程仅管理员可见，其余用户访问返回 404（§3.2）。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.rbac import get_current_user, get_current_user_optional, require_builder
from app.storage.db import get_db
from app.storage.models import Chapter, Course, Major, User

router = APIRouter(tags=["courses"])

LIST_DEFAULT_SIZE = 20
LIST_MAX_SIZE = 100


# ---------------------------------------------------------------------------
# 专业
# ---------------------------------------------------------------------------


@router.get("/majors")
async def list_majors(db: Annotated[AsyncSession, Depends(get_db)]):
    """专业列表（含各专业的课程空间数）。专业名单由管理员导入（§4.1）。"""
    course_counts = (
        select(Course.major_id, func.count().label("n"))
        .where(Course.scope == "public", Course.status == "active")
        .group_by(Course.major_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Major, course_counts.c.n)
            .outerjoin(course_counts, course_counts.c.major_id == Major.id)
            .order_by(Major.id)
        )
    ).all()
    items = [
        {"id": m.id, "name": m.name, "code": m.code, "course_count": n or 0}
        for m, n in rows
    ]
    return {"total": len(items), "items": items}


# ---------------------------------------------------------------------------
# 课程空间
# ---------------------------------------------------------------------------


class CreateCourseRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    major_id: int
    description: str | None = Field(default=None, max_length=512)


def _course_brief(course: Course, major_name: str | None = None) -> dict:
    return {
        "id": course.id,
        "name": course.name,
        "description": course.description,
        "status": course.status,
        "major_id": course.major_id,
        "major_name": major_name,
        "created_at": course.created_at.isoformat() if course.created_at else None,
    }


@router.get("/courses")
async def list_courses(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
    major_id: int | None = None,
    status: str | None = Query(default=None, pattern="^(active|pending|disabled)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=LIST_DEFAULT_SIZE, ge=1, le=LIST_MAX_SIZE),
):
    """课程空间列表（分页约定 §12：page/size，响应含 total/items）。

    默认仅列出 public + active；status=pending/disabled 仅管理员可查。
    """
    stmt = select(Course).where(Course.scope == "public")
    if status is None or status == "active":
        stmt = stmt.where(Course.status == "active")
    else:
        if user is None or user.role != "admin":
            raise HTTPException(status_code=404, detail="Not Found")
        stmt = stmt.where(Course.status == status)
    if major_id is not None:
        stmt = stmt.where(Course.major_id == major_id)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Course.id).offset((page - 1) * size).limit(size)
        )
    ).scalars().all()
    major_names = {
        m.id: m.name
        for m in (
            await db.execute(select(Major).where(Major.id.in_({c.major_id for c in rows})))
        ).scalars()
    } if rows else {}
    return {
        "total": total,
        "items": [_course_brief(c, major_names.get(c.major_id)) for c in rows],
    }


@router.post("/courses", status_code=201)
async def create_course(
    payload: CreateCourseRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """创建课程空间：管理员直接开通（active）；学生提交为待审批申请（pending，§4.1）。"""
    major = await db.get(Major, payload.major_id)
    if major is None:
        raise HTTPException(status_code=422, detail="专业不存在")
    status = "active" if user.role == "admin" else "pending"
    course = Course(
        scope="public",
        major_id=payload.major_id,
        name=payload.name,
        description=payload.description,
        status=status,
    )
    db.add(course)
    await db.commit()
    return {**_course_brief(course, major.name), "applied": status == "pending"}


@router.get("/courses/{course_id}")
async def get_course(
    course_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """课程空间主页数据。pending/disabled 仅管理员可见，其余 404（数据级权限 §3.2）。"""
    course = await db.get(Course, course_id)
    if course is None or course.scope != "public":
        raise HTTPException(status_code=404, detail="Not Found")
    if course.status != "active" and (user is None or user.role != "admin"):
        raise HTTPException(status_code=404, detail="Not Found")
    major = await db.get(Major, course.major_id) if course.major_id else None
    chapter_count = (
        await db.execute(
            select(func.count()).select_from(Chapter).where(Chapter.course_id == course.id)
        )
    ).scalar_one()
    return {**_course_brief(course, major.name if major else None),
            "chapter_count": chapter_count}


# ---------------------------------------------------------------------------
# 章节知识树
# ---------------------------------------------------------------------------


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


@router.get("/courses/{course_id}/chapters")
async def get_chapters(course_id: int, db: Annotated[AsyncSession, Depends(get_db)]):
    """章节知识树（结构化 RAG 骨架，§4.1）。"""
    course = await db.get(Course, course_id)
    if course is None or course.scope != "public":
        raise HTTPException(status_code=404, detail="Not Found")
    chapters = (
        await db.execute(
            select(Chapter).where(Chapter.course_id == course_id)
        )
    ).scalars().all()
    return {"course_id": course_id, "items": _build_tree(chapters)}


class CreateChapterRequest(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    parent_id: int | None = None
    order_idx: int = 0


@router.post("/courses/{course_id}/chapters", status_code=201)
async def create_chapter(
    course_id: int,
    payload: CreateChapterRequest,
    user: Annotated[User, Depends(require_builder)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """创建章节/小节（builder/admin，§3.2 共建者维护章节树）。"""
    course = await db.get(Course, course_id)
    if course is None or course.scope != "public" or course.status != "active":
        raise HTTPException(status_code=404, detail="Not Found")
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
