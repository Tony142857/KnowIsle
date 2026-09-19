"""页面路由（§13）：服务端渲染入口，按板块拆分到本包内各模块。

v0.2 落地：登录页 / 专业列表页 / 课程空间页（含章节树侧边栏）；
个人知识库（v0.3）、社区板块（v0.5+）、个人中心（v0.6）仍为占位页。
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
from app.storage.models import Chapter, Course, Major, User

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
    "library": ("个人知识库", "v0.3", "私有资料上传 → 结构化 → 问答 / 溯源 / 大纲 / 习题 / 串讲", "library"),
    "me": ("个人中心", "v0.6", "成长看板 / 我的上传与审核进度 / 收藏关注 / 通知 / AI 额度与 Key 配置", ""),
}


def _ctx(user: User | None, **extra) -> dict:
    return {"user": user, **extra}


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
    """板块占位页：/library、/me（其余路径交给 FastAPI 默认 404）。"""
    if page not in PLACEHOLDER_PAGES:
        raise HTTPException(status_code=404)
    name, version, note, active = PLACEHOLDER_PAGES[page]
    return templates.TemplateResponse(
        request, "placeholder.html",
        _ctx(user, name=name, version=version, note=note, active=active),
    )
