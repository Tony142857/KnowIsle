"""页面路由（§13）：服务端渲染入口，按板块拆分到本包内各模块。

v0.3 落地：登录页 / 专业列表页 / 课程空间页（含章节树侧边栏）/
个人知识库（库树管理 + 上传轮询）/ AI 对话页 / 原文溯源页；
社区板块（v0.5+）、个人中心（v0.6）仍为占位页。
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.identity.rbac import get_current_user_optional
from app.identity.session import destroy_session
from app.storage.cache import get_redis
from app.storage.db import get_db
from app.storage.models import Chapter, Chunk, Course, Document, Major, Semester, User

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")

# 社区板块（§4.2），用于 /boards/{board} 校验
BOARDS = {
    "qa": ("知屿问答", "v0.5"),
    "experience": ("经验长廊", "v0.7"),
    "bounty": ("资料求援", "v0.7"),
    "discuss": ("讨论区", "v0.5"),
}

# 板块占位页配置：路由 → (板块名, 交付版本, 说明, 导航高亮键)
PLACEHOLDER_PAGES = {
    "me": ("个人中心", "v0.6", "成长看板 / 我的上传与审核进度 / 收藏关注 / 通知 / AI 额度与 Key 配置", ""),
}


def _ctx(user: User | None, **extra) -> dict:
    return {"user": user, **extra}


def _login_redirect(user: User | None) -> RedirectResponse | None:
    """需登录页面的统一处理：未登录 303 重定向到 /login。"""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return None


@router.get("/")
async def index(request: Request, user: Annotated[User | None, Depends(get_current_user_optional)]):
    return templates.TemplateResponse(request, "index.html", _ctx(user, active=""))


@router.get("/login")
async def login(request: Request, user: Annotated[User | None, Depends(get_current_user_optional)]):
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        _ctx(user, active="", college_name=get_settings().college_name),
    )


@router.get("/logout")
async def logout(request: Request):
    """页面端退出：销毁 Redis 会话、清 Cookie、回首页。"""
    settings = get_settings()
    await destroy_session(get_redis(), request.cookies.get(settings.session_cookie_name, ""))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(settings.session_cookie_name)
    return resp


# ---------------------------------------------------------------------------
# 课程空间板块（§4.1 / §13）：专业列表 → 专业课程 → 课程空间页
# ---------------------------------------------------------------------------


@router.get("/majors")
async def majors(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
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
    return templates.TemplateResponse(
        request, "majors.html",
        _ctx(user, active="majors", majors=items,
             college_name=get_settings().college_name),
    )


@router.get("/majors/{major_id}")
async def major_detail(
    major_id: int,
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    major = await db.get(Major, major_id)
    if major is None:
        raise HTTPException(status_code=404)
    courses = (
        await db.execute(
            select(Course)
            .where(Course.scope == "public", Course.major_id == major_id,
                   Course.status == "active")
            .order_by(Course.id)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request, "major_detail.html",
        _ctx(user, active="majors", major=major, courses=courses),
    )


@router.get("/courses/{course_id}")
async def course_detail(
    course_id: int,
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """课程空间页：章节树侧边栏 + Tab（资料库 / AI 问答 / 讨论 / 贡献榜，后续迭代填充）。"""
    course = await db.get(Course, course_id)
    if course is None or course.scope != "public":
        raise HTTPException(status_code=404)
    if course.status != "active" and (user is None or user.role != "admin"):
        raise HTTPException(status_code=404)
    major = await db.get(Major, course.major_id) if course.major_id else None
    chapters = (
        await db.execute(select(Chapter).where(Chapter.course_id == course_id))
    ).scalars().all()

    nodes = {c.id: {"chapter": c, "children": []} for c in chapters}
    roots = []
    for c in sorted(chapters, key=lambda x: (x.order_idx, x.id)):
        if c.parent_id is not None and c.parent_id in nodes:
            nodes[c.parent_id]["children"].append(nodes[c.id])
        else:
            roots.append(nodes[c.id])

    can_moderate = user is not None and user.role in ("builder", "admin")
    return templates.TemplateResponse(
        request, "course_detail.html",
        _ctx(user, active="majors", course=course, major=major,
             chapter_tree=roots, can_moderate=can_moderate),
    )


# ---------------------------------------------------------------------------
# 个人知识库板块（§4.3 / §13）：库树管理 + 上传 → AI 问答 → 原文溯源
# 注意：/library 为单段路径，必须先于 /{page} 通配占位注册。
# ---------------------------------------------------------------------------


@router.get("/library")
async def library(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """个人知识库页：学期 → 课程 → 文档 三级库树，服务端渲染首屏。"""
    if (resp := _login_redirect(user)) is not None:
        return resp
    semesters = (
        await db.execute(
            select(Semester).where(Semester.owner_id == user.id).order_by(Semester.id)
        )
    ).scalars().all()
    courses = (
        await db.execute(
            select(Course)
            .where(Course.scope == "personal", Course.owner_id == user.id)
            .order_by(Course.id)
        )
    ).scalars().all()

    chapter_map: dict[int, list[Chapter]] = {}
    doc_map: dict[int, list[Document]] = {}
    course_ids = [c.id for c in courses]
    if course_ids:
        chapters = (
            await db.execute(
                select(Chapter)
                .where(Chapter.course_id.in_(course_ids))
                .order_by(Chapter.order_idx, Chapter.id)
            )
        ).scalars().all()
        for ch in chapters:
            chapter_map.setdefault(ch.course_id, []).append(ch)
        documents = (
            await db.execute(
                select(Document)
                .where(Document.course_id.in_(course_ids))
                .order_by(Document.created_at.desc(), Document.id.desc())
            )
        ).scalars().all()
        for d in documents:
            doc_map.setdefault(d.course_id, []).append(d)

    def course_item(c: Course) -> dict:
        return {
            "course": c,
            "chapters": chapter_map.get(c.id, []),
            "documents": doc_map.get(c.id, []),
        }

    semester_groups = [
        {
            "semester": s,
            "courses": [course_item(c) for c in courses if c.semester_id == s.id],
        }
        for s in semesters
    ]
    uncategorized = [course_item(c) for c in courses if c.semester_id is None]
    return templates.TemplateResponse(
        request, "library.html",
        _ctx(user, active="library", semester_groups=semester_groups,
             uncategorized=uncategorized, semesters=semesters),
    )


@router.get("/library/chat")
async def library_chat(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
    course_id: int | None = None,
):
    """AI 对话页：SSE 流式问答 + 引用溯源（scope 三态切换）。"""
    if (resp := _login_redirect(user)) is not None:
        return resp
    if course_id is None:
        return RedirectResponse("/library", status_code=303)
    course = await db.get(Course, course_id)
    if course is None or course.scope != "personal" or course.owner_id != user.id:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request, "library_chat.html",
        _ctx(user, active="library", course=course),
    )


@router.get("/documents/{document_id}/source")
async def document_source(
    document_id: int,
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """原文溯源页：按 chunk 渲染解析文本，URL hash 定位高亮（仅本人/管理员）。"""
    if (resp := _login_redirect(user)) is not None:
        return resp
    doc = await db.get(Document, document_id)
    if doc is None or (doc.owner_id != user.id and user.role != "admin"):
        raise HTTPException(status_code=404)
    chunks = (
        await db.execute(
            select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.id)
        )
    ).scalars().all()
    course = await db.get(Course, doc.course_id)
    return templates.TemplateResponse(
        request, "document_source.html",
        _ctx(user, active="library", doc=doc, course=course, chunks=chunks),
    )


# ---------------------------------------------------------------------------
# 占位页（未交付板块）
# ---------------------------------------------------------------------------


@router.get("/boards/{board}")
async def board(
    board: str,
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
):
    if board not in BOARDS:
        raise HTTPException(status_code=404)
    name, version = BOARDS[board]
    return templates.TemplateResponse(
        request, "placeholder.html",
        _ctx(user, name=name, version=version, note="", active=board),
    )


@router.get("/{page}")
async def placeholder_page(
    page: str,
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """板块占位页：/me（其余路径交给 FastAPI 默认 404）。"""
    if page not in PLACEHOLDER_PAGES:
        raise HTTPException(status_code=404)
    name, version, note, active = PLACEHOLDER_PAGES[page]
    return templates.TemplateResponse(
        request, "placeholder.html",
        _ctx(user, name=name, version=version, note=note, active=active),
    )
