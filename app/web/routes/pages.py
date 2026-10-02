"""页面路由（§13）：服务端渲染入口，按板块拆分到本包内各模块。

v0.3 落地：登录页 / 专业列表页 / 课程空间页（含章节树侧边栏）/
个人知识库（库树管理 + 上传轮询）/ AI 对话页 / 原文溯源页；
v0.4 落地：公共库投稿入口（library 内）/ 资源详情页 / 协审工作台 / 管理员终审页；
v0.5 落地：社区板块（问答/讨论列表、发帖、帖子详情含 AI 首答与评论树）、
个人中心（成长看板 / AI 额度与 Key / 通知）。
v0.6 落地：管理后台主页（/admin：空间管理 / 用户治理 / 平台配置）、
课程关注与资源/帖子收藏按钮的状态注入。
v0.8 落地：全站搜索页（/search，骨架 SSR + 客户端 fetch 结果）。
复习工具页（/library/review，模块 A4）：大纲 / 习题生成，数据由前端 fetch。
"""

import logging
import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import any_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.community.experience import can_feature
from app.community.posts import (
    ai_answer_key,
    build_ai_citations,
    derive_ai_answer_status,
    derive_summary_status,
    exp_summary_key,
    make_excerpt,
)
from app.config import get_settings
from app.identity.growth import LEVEL_THRESHOLDS
from app.identity.rbac import get_current_user_optional
from app.identity.session import destroy_session
from app.storage.cache import get_redis
from app.storage.db import get_db
from app.storage.models import (
    Chapter,
    Chunk,
    Comment,
    Course,
    Document,
    Favorite,
    Follow,
    Major,
    Post,
    Resource,
    ResourceRating,
    ReviewRecord,
    ReviewTask,
    Semester,
    User,
    Vote,
)

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")

# 社区板块（§4.2），用于 /boards/{board} 校验
BOARDS = {
    "qa": ("知屿问答", "v0.5"),
    "experience": ("经验长廊", "v0.7"),
    "bounty": ("资料求援", "v0.7"),
    "discuss": ("讨论区", "v0.5"),
}

# 已落地的社区板块（v0.7 起四板块全部开放）
OPEN_BOARDS = ("qa", "discuss", "experience", "bounty")

BOARD_PAGE_SIZE = 20

# 与 community/posts.py 的 chunk_id 引用格式一致（[per_d12_00034] 式）
_AI_CITE_RE = re.compile(r"\[((?:per|pub)_d\d+_\d{4,})\]")


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
    follower_count = (
        await db.execute(
            select(func.count()).select_from(Follow).where(
                Follow.target_type == "course", Follow.target_id == course_id
            )
        )
    ).scalar_one()
    is_following = False
    if user is not None:
        is_following = (
            await db.execute(
                select(Follow.id).where(
                    Follow.user_id == user.id,
                    Follow.target_type == "course",
                    Follow.target_id == course_id,
                )
            )
        ).scalar_one_or_none() is not None
    return templates.TemplateResponse(
        request, "course_detail.html",
        _ctx(user, active="majors", course=course, major=major,
             chapter_tree=roots, can_moderate=can_moderate,
             is_following=is_following, follower_count=follower_count),
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

    # 本人文档的最新投稿记录（每个 document 取最新一条），用于渲染投稿状态徽章
    doc_ids = [d.id for docs in doc_map.values() for d in docs]
    resource_map: dict[int, dict] = {}
    if doc_ids:
        submissions = (
            await db.execute(
                select(Resource)
                .where(Resource.document_id.in_(doc_ids))
                .order_by(Resource.created_at.desc(), Resource.id.desc())
            )
        ).scalars().all()
        for r in submissions:
            resource_map.setdefault(
                r.document_id,
                {"review_status": r.review_status, "resource_id": r.id},
            )
    return templates.TemplateResponse(
        request, "library.html",
        _ctx(user, active="library", semester_groups=semester_groups,
             uncategorized=uncategorized, semesters=semesters,
             resource_map=resource_map),
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


@router.get("/library/review")
async def library_review(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    course_id: int | None = None,
):
    """复习工具页（模块 A4，§13）：大纲 / 习题生成；课程与章节由前端 fetch
    （/api/library/ 个人课程 + /api/courses 公共课程），生成走 /api/review/*。
    course_id 仅作客户端预选，权限由 API 层校验，本路由不做服务端校验。"""
    if (resp := _login_redirect(user)) is not None:
        return resp
    return templates.TemplateResponse(
        request, "library_review.html",
        _ctx(user, active="library", course_id=course_id),
    )


@router.get("/documents/{document_id}/source")
async def document_source(
    document_id: int,
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """原文溯源页：按 chunk 渲染解析文本，URL hash 定位高亮
    （public 文档任何登录用户可见，个人文档仅本人/管理员）。"""
    if (resp := _login_redirect(user)) is not None:
        return resp
    doc = await db.get(Document, document_id)
    if doc is None or (
        doc.scope != "public" and doc.owner_id != user.id and user.role != "admin"
    ):
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
# 公共库板块（v0.4）：资源详情页 / 协审工作台 / 管理员终审页
# 注意：均为固定段或多段路径，先于文件末尾 /{page} 通配占位注册。
# ---------------------------------------------------------------------------


async def _public_copy_of(db: AsyncSession, doc: Document) -> Document | None:
    """终审通过时复制派生的 public 副本（storage_key 复用原件，凭此定位）。"""
    return (
        await db.execute(
            select(Document).where(
                Document.scope == "public", Document.storage_key == doc.storage_key
            )
        )
    ).scalars().first()


@router.get("/resources/{resource_id}")
async def resource_detail(
    resource_id: int,
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """资源详情页：评分 / 下载 / 在线预览 / 原文溯源入口。

    可见性与 API 一致（§3.2）：approved 任何登录用户 / 本人投稿 / admin，其余 404。
    """
    if (resp := _login_redirect(user)) is not None:
        return resp
    resource = await db.get(Resource, resource_id)
    if resource is None or (
        resource.review_status != "approved"
        and resource.uploader_id != user.id
        and user.role != "admin"
    ):
        raise HTTPException(status_code=404)
    doc = await db.get(Document, resource.document_id)
    uploader = await db.get(User, resource.uploader_id)
    course = await db.get(Course, resource.course_id)
    chapter = await db.get(Chapter, resource.chapter_id) if resource.chapter_id else None
    my_rating = (
        await db.execute(
            select(ResourceRating.stars).where(
                ResourceRating.resource_id == resource.id,
                ResourceRating.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    preview_available = doc.file_type in ("pdf_textbook", "markdown")
    if not preview_available:
        public_doc = await _public_copy_of(db, doc)
        preview_available = public_doc is not None and public_doc.preview_key is not None
    is_favorited = (
        await db.execute(
            select(Favorite.id).where(
                Favorite.user_id == user.id,
                Favorite.target_type == "resource",
                Favorite.target_id == resource.id,
            )
        )
    ).scalar_one_or_none() is not None
    return templates.TemplateResponse(
        request, "resource_detail.html",
        _ctx(user, active="majors", resource=resource, doc=doc, uploader=uploader,
             course=course, chapter=chapter, my_rating=my_rating,
             preview_available=preview_available, is_favorited=is_favorited),
    )


async def _records_with_names_batch(
    db: AsyncSession, task_ids: list[int]
) -> dict[int, list[dict]]:
    """批量取多个任务的协审/终审意见（含协审员昵称），按 task_id 分组（v0.9：消 N+1）。

    records 一次 IN 批量查、users 一次 IN 批量查（参考 api/admin.py 审核任务总览写法）。
    """
    if not task_ids:
        return {}
    records = (
        await db.execute(
            select(ReviewRecord).where(ReviewRecord.task_id.in_(task_ids))
            .order_by(ReviewRecord.id)
        )
    ).scalars().all()
    names = {
        u.id: u.nickname
        for u in (
            await db.execute(
                select(User).where(User.id.in_({r.reviewer_id for r in records}))
            )
        ).scalars()
    } if records else {}
    grouped: dict[int, list[dict]] = {task_id: [] for task_id in task_ids}
    for r in records:
        grouped[r.task_id].append(
            {
                "reviewer": names.get(r.reviewer_id, "协审员"),
                "stage": r.stage,
                "verdict": r.verdict,
                "comment": r.comment,
                "created_at": r.created_at,
            }
        )
    return grouped


async def _records_with_names(db: AsyncSession, task_id: int) -> list[dict]:
    """某任务的协审/终审意见（含协审员昵称，按提交顺序）。"""
    return (await _records_with_names_batch(db, [task_id])).get(task_id, [])


@router.get("/review")
async def review_workbench(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
    task: int | None = None,
):
    """协审工作台（reviewer/admin）：本人被指派的 co_review 任务队列 + 裁决。"""
    if (resp := _login_redirect(user)) is not None:
        return resp
    if user.role not in ("reviewer", "admin"):
        raise HTTPException(status_code=404)
    rows = (
        await db.execute(
            select(ReviewTask, Resource, Course.name)
            .join(Resource, Resource.id == ReviewTask.resource_id)
            .join(Course, Course.id == Resource.course_id)
            .where(
                ReviewTask.stage == "co_review",
                user.id == any_(ReviewTask.assignee_ids),
            )
            .order_by(ReviewTask.created_at)
        )
    ).all()
    tasks = [
        {
            "task_id": t.id,
            "created_at": t.created_at,
            "resource": {
                "id": r.id, "title": r.title,
                "course_id": r.course_id, "course_name": course_name,
            },
        }
        for t, r, course_name in rows
    ]

    selected = None
    if task is not None:
        hit = next((row for row in rows if row[0].id == task), None)
        if hit is None:
            raise HTTPException(status_code=404)
        t, r, course_name = hit
        doc = await db.get(Document, r.document_id)
        chapter = await db.get(Chapter, r.chapter_id) if r.chapter_id else None
        selected = {
            "task_id": t.id,
            "precheck": t.precheck_result,
            "created_at": t.created_at,
            "resource": r,
            "course_name": course_name,
            "chapter_title": chapter.title if chapter else None,
            "doc": doc,
            "records": await _records_with_names(db, t.id),
            "preview_url": f"/api/resources/{r.id}/preview",
        }
    return templates.TemplateResponse(
        request, "review.html",
        _ctx(user, active="review", tasks=tasks, selected=selected),
    )


@router.get("/admin")
async def admin_home(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """管理后台主页（admin，v0.6）：审核终审入口 / 空间管理 / 用户治理 / 平台配置。
    数据均由前端 fetch 管理 API 渲染，本路由仅做门控与骨架渲染。"""
    if (resp := _login_redirect(user)) is not None:
        return resp
    if user.role != "admin":
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "admin.html", _ctx(user, active="admin"))


@router.get("/admin/review")
async def admin_review(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """管理员终审页（admin）：final 任务队列（含协审意见聚合与预览）+
    等待协审的 co_review 任务（只读）+ 最近完成的 done 任务。"""
    if (resp := _login_redirect(user)) is not None:
        return resp
    if user.role != "admin":
        raise HTTPException(status_code=404)

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

    final_rows = await _query(["final"])
    # 协审/终审意见按 task_ids 一次 IN 批量查（v0.9：原实现每任务两条查询，N+1）
    records_map = await _records_with_names_batch(db, [t.id for t, *_ in final_rows])
    final_tasks = [
        {
            "task_id": t.id,
            "precheck": t.precheck_result,
            "created_at": t.created_at,
            "resource": r,
            "course_name": course_name,
            "uploader_nickname": nickname,
            "records": records_map[t.id],
            "preview_url": f"/api/resources/{r.id}/preview",
        }
        for t, r, course_name, nickname in final_rows
    ]
    co_tasks = [
        {"task_id": t.id, "created_at": t.created_at,
         "resource": r, "course_name": course_name,
         "uploader_nickname": nickname,
         "assignee_count": len(t.assignee_ids or [])}
        for t, r, course_name, nickname in await _query(["co_review"])
    ]
    done_tasks = [
        {"task_id": t.id, "finished_at": t.finished_at,
         "resource": r, "course_name": course_name}
        for t, r, course_name, nickname in await _query(["done"], 20)
    ]
    return templates.TemplateResponse(
        request, "admin_review.html",
        _ctx(user, active="admin_review", final_tasks=final_tasks,
             co_tasks=co_tasks, done_tasks=done_tasks),
    )


# ---------------------------------------------------------------------------
# 社区板块（v0.5）：板块帖子列表 / 发帖 / 帖子详情（AI 首答 + 评论树）
# 注意：/posts/new 必须先于 /posts/{post_id} 注册；两者均先于 /{page} 通配。
# ---------------------------------------------------------------------------


@router.get("/boards/{board}")
async def board(
    board: str,
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
    tag: str | None = None,
    grade: str | None = None,
    featured: str | None = None,
    page: int = 1,
):
    """板块帖子列表（v0.7 四板块全部 SSR）：经验长廊附标签云/届别过滤/精华区，
    资料求援卡片展示悬赏分；DB 不可用时降级为空列表 + 客户端补载。"""
    if board not in BOARDS:
        raise HTTPException(status_code=404)
    name = BOARDS[board][0]

    page = max(page, 1)
    total = 0
    items: list[dict] = []
    tag_cloud: list[dict] = []
    grades: list[str] = []
    ssr_ok = True
    try:
        filters = [Post.board == board, Post.status.in_(["normal", "featured"])]
        if tag:
            filters.append(Post.tags.contains([tag]))
        if grade:
            filters.append(User.grade == grade)
        if featured == "1":
            filters.append(Post.status == "featured")
        total = (
            await db.execute(
                select(func.count(Post.id))
                .join(User, User.id == Post.author_id)
                .where(*filters)
            )
        ).scalar_one()
        # 先取本页帖子，聚合只对这页做 IN 过滤（v0.9：原实现对 comments/votes
        # 全表 GROUP BY 再 JOIN 本页 20 条，表越大越慢）
        rows = (
            await db.execute(
                select(Post, User)
                .join(User, User.id == Post.author_id)
                .where(*filters)
                .order_by(Post.created_at.desc())
                .offset((page - 1) * BOARD_PAGE_SIZE)
                .limit(BOARD_PAGE_SIZE)
            )
        ).all()
        post_ids = [post.id for post, _ in rows]
        comment_counts: dict[int, int] = {}
        vote_scores: dict[int, int] = {}
        if post_ids:
            comment_counts = dict(
                (
                    await db.execute(
                        select(Comment.post_id, func.count())
                        .where(Comment.post_id.in_(post_ids))
                        .group_by(Comment.post_id)
                    )
                ).all()
            )
            vote_scores = dict(
                (
                    await db.execute(
                        select(Vote.target_id, func.sum(Vote.value))
                        .where(Vote.target_type == "post", Vote.target_id.in_(post_ids))
                        .group_by(Vote.target_id)
                    )
                ).all()
            )
        items = [
            {
                "id": post.id,
                "title": post.title,
                "author": author,
                "tags": post.tags or [],
                "view_count": post.view_count,
                "comment_count": comment_counts.get(post.id, 0),
                "vote_score": vote_scores.get(post.id, 0),
                "status": post.status,
                "bounty_score": post.bounty_score,
                "ai_summary": post.ai_summary,
                "has_ai_answer": post.ai_first_answer is not None,
                "has_accepted": post.accepted_comment_id is not None,
                "created_at": post.created_at,
                "excerpt": make_excerpt(post.content),
            }
            for post, author in rows
        ]
        if board == "experience":
            # 标签云（§13 经验长廊页）：该板块全部帖的标签频次，按热度排序取前 12
            rows_tags = (
                await db.execute(
                    select(Post.tags).where(
                        Post.board == "experience",
                        Post.status.in_(["normal", "featured"]),
                        Post.tags.is_not(None),
                    )
                )
            ).scalars().all()
            counts: dict[str, int] = {}
            for tags in rows_tags:
                for t in tags or []:
                    counts[t] = counts.get(t, 0) + 1
            tag_cloud = [
                {"tag": t, "count": n}
                for t, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:12]
            ]
            # 届别过滤（§13）：该板块作者年级去重（如 2023 级），供页面渲染过滤链接
            grade_rows = (
                await db.execute(
                    select(User.grade)
                    .join(Post, Post.author_id == User.id)
                    .where(
                        Post.board == "experience",
                        Post.status.in_(["normal", "featured"]),
                        User.grade.is_not(None),
                    )
                    .distinct()
                    .order_by(User.grade)
                )
            ).scalars().all()
            grades = [g for g in grade_rows if g]
    except Exception:
        # DB 不可用（如 CI 冒烟环境）：页面骨架照常渲染，列表由客户端 fetch 补载
        logger.warning("板块列表查询失败，降级客户端加载 board=%s", board, exc_info=True)
        ssr_ok = False
    return templates.TemplateResponse(
        request, "board.html",
        _ctx(user, active=board, board=board, board_name=name, posts=items,
             total=total, page=page, size=BOARD_PAGE_SIZE, tag=tag or "",
             grade=grade or "", featured=featured == "1", tag_cloud=tag_cloud,
             grades=grades, ssr_ok=ssr_ok),
    )


@router.get("/posts/new")
async def post_new(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    board: str | None = None,
):
    """发帖页（登录）：qa 帖必选公共课程（+可选章节）；experience 帖走结构化模板；
    bounty 帖须填悬赏贡献分；discuss 帖无附加要求。"""
    if (resp := _login_redirect(user)) is not None:
        return resp
    if board not in OPEN_BOARDS:
        board = "qa"
    return templates.TemplateResponse(
        request, "post_new.html",
        _ctx(user, active=board, board=board, board_name=BOARDS[board][0]),
    )


def _ai_segments(answer: str, citations: list[dict]) -> list[dict]:
    """AI 首答正文切片：普通文本段 + [chunk_id] 引用段（模板据此渲染溯源链接）。"""
    doc_of = {c["chunk_id"]: c["document_id"] for c in citations}
    segments: list[dict] = []
    pos = 0
    for m in _AI_CITE_RE.finditer(answer):
        document_id = doc_of.get(m.group(1))
        if document_id is None:
            continue
        if m.start() > pos:
            segments.append({"text": answer[pos:m.start()]})
        segments.append({"cite": m.group(1), "document_id": document_id})
        pos = m.end()
    if pos < len(answer):
        segments.append({"text": answer[pos:]})
    return segments


@router.get("/posts/{post_id}")
async def post_detail(
    post_id: int,
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """帖子详情（匿名可读）：SSR 正文 + 评论树（楼中楼）+ AI 首答卡；
    每次访问 view_count+1（与 GET /api/posts/{id} 一致）。"""
    post = await db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404)
    post.view_count += 1

    author = await db.get(User, post.author_id)
    course = await db.get(Course, post.course_id) if post.course_id else None
    chapter = await db.get(Chapter, post.chapter_id) if post.chapter_id else None
    vote_score = (
        await db.execute(
            select(func.coalesce(func.sum(Vote.value), 0)).where(
                Vote.target_type == "post", Vote.target_id == post.id
            )
        )
    ).scalar_one()

    # 评论平铺查询 → 按 parent_id 组装树（已采纳评论顶到最前）
    rows = (
        await db.execute(
            select(Comment, User)
            .join(User, User.id == Comment.author_id)
            .where(Comment.post_id == post.id)
            .order_by(Comment.created_at)
        )
    ).all()
    comment_ids = [c.id for c, _ in rows]
    scores: dict[int, int] = {}
    my_votes: dict[int, int] = {}
    if comment_ids:
        score_rows = (
            await db.execute(
                select(Vote.target_id, func.sum(Vote.value))
                .where(Vote.target_type == "comment", Vote.target_id.in_(comment_ids))
                .group_by(Vote.target_id)
            )
        ).all()
        scores = dict(score_rows)
    if user is not None:
        my_vote = (
            await db.execute(
                select(Vote.value).where(
                    Vote.user_id == user.id,
                    Vote.target_type == "post",
                    Vote.target_id == post.id,
                )
            )
        ).scalar_one_or_none()
        my_post_vote = my_vote or 0
        if comment_ids:
            mine = (
                await db.execute(
                    select(Vote.target_id, Vote.value).where(
                        Vote.user_id == user.id,
                        Vote.target_type == "comment",
                        Vote.target_id.in_(comment_ids),
                    )
                )
            ).all()
            my_votes = dict(mine)
    else:
        my_post_vote = 0

    nodes = {
        c.id: {
            "comment": c, "author": a,
            "score": scores.get(c.id, 0), "my_vote": my_votes.get(c.id, 0),
            "children": [],
        }
        for c, a in rows
    }
    roots = []
    for c, _ in rows:
        node = nodes[c.id]
        if c.parent_id is not None and c.parent_id in nodes:
            nodes[c.parent_id]["children"].append(node)
        else:
            roots.append(node)
    if post.accepted_comment_id is not None and post.accepted_comment_id in nodes:
        accepted_node = nodes[post.accepted_comment_id]
        if accepted_node in roots:
            roots.remove(accepted_node)
            roots.insert(0, accepted_node)

    # AI 首答状态与引用（仅 qa 帖）
    ai_status = "none"
    ai_citations: list[dict] = []
    ai_segments: list[dict] = []
    if post.board == "qa":
        redis_state = None
        if not post.ai_first_answer:
            try:
                redis_state = await get_redis().get(ai_answer_key(post.id))
            except Exception:
                # Redis 故障仅记日志放行：按无状态键处理（与项目容错惯例一致）
                logger.warning("AI 首答状态键读取失败 post_id=%s", post.id, exc_info=True)
        ai_status = derive_ai_answer_status(
            post.board, post.ai_first_answer, redis_state
        )
        if post.ai_first_answer:
            ai_citations = await build_ai_citations(db, post.ai_first_answer)
            ai_segments = _ai_segments(post.ai_first_answer, ai_citations)

    # 经验帖 AI 摘要状态（v0.7）：done 渲染摘要 / pending 轮询 / failed 友好提示
    summary_status = "none"
    if post.board == "experience":
        summary_state = None
        if not post.ai_summary:
            try:
                summary_state = await get_redis().get(exp_summary_key(post.id))
            except Exception:
                # Redis 故障仅记日志放行：按无状态键处理（与项目容错惯例一致）
                logger.warning("AI 摘要状态键读取失败 post_id=%s", post.id, exc_info=True)
        summary_status = derive_summary_status(post.board, post.ai_summary, summary_state)

    await db.commit()
    is_favorited = False
    if user is not None:
        is_favorited = (
            await db.execute(
                select(Favorite.id).where(
                    Favorite.user_id == user.id,
                    Favorite.target_type == "post",
                    Favorite.target_id == post.id,
                )
            )
        ).scalar_one_or_none() is not None
    return templates.TemplateResponse(
        request, "post_detail.html",
        _ctx(user, active=post.board, post=post, author=author, course=course,
             chapter=chapter, vote_score=vote_score, my_vote=my_post_vote,
             comment_tree=roots, comment_count=len(rows),
             is_author=user is not None and user.id == post.author_id,
             is_favorited=is_favorited,
             can_feature=(user is not None and post.board == "experience"
                          and can_feature(user, post.author_id)),
             ai_status=ai_status, ai_citations=ai_citations,
             ai_segments=ai_segments, summary_status=summary_status),
    )


# ---------------------------------------------------------------------------
# 个人中心（v0.5）：成长看板 / 积分明细 / AI 额度与自定义 Key / 站内通知
# ---------------------------------------------------------------------------


@router.get("/me")
async def me(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """个人中心（登录）：SSR 注入等级进度，额度/明细/通知由前端 fetch 渲染。"""
    if (resp := _login_redirect(user)) is not None:
        return resp
    major = await db.get(Major, user.major_id) if user.major_id else None

    level = user.level
    floor = LEVEL_THRESHOLDS[min(level, len(LEVEL_THRESHOLDS)) - 1]
    if level < len(LEVEL_THRESHOLDS):
        ceil = LEVEL_THRESHOLDS[level]
        progress = min(100, max(0, round((user.score - floor) / (ceil - floor) * 100)))
        next_threshold: int | None = ceil
        remaining = max(ceil - user.score, 0)
    else:
        progress = 100
        next_threshold = None
        remaining = 0
    growth = {
        "level": level,
        "next_threshold": next_threshold,
        "remaining": remaining,
        "progress": progress,
    }
    return templates.TemplateResponse(
        request, "me.html",
        _ctx(user, active="", major=major, growth=growth),
    )


# ---------------------------------------------------------------------------
# 全站搜索（v0.8）：骨架 SSR（过滤条件回显），结果由客户端 fetch /api/search 渲染
# （检索交互重，且搜索查询逻辑由后端 API 独立实现，页面端不重复 SSR 查询）
# ---------------------------------------------------------------------------


@router.get("/search")
async def search_page(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    q: str = "",
    type: str = "all",
    board: str = "",
    course_id: int | None = None,
):
    """全站搜索页（登录）：关键词 + 类型（all/post/resource）+ 板块 + 课程过滤。"""
    if (resp := _login_redirect(user)) is not None:
        return resp
    if type not in ("all", "post", "resource"):
        type = "all"
    if board not in BOARDS:
        board = ""
    return templates.TemplateResponse(
        request, "search.html",
        _ctx(user, active="search", q=q, search_type=type, board=board,
             course_id=course_id, boards=BOARDS),
    )


# ---------------------------------------------------------------------------
# 占位页（未交付板块）；/{page} 为通配兜底，必须最后注册
# ---------------------------------------------------------------------------


@router.get("/{page}")
async def placeholder_page(
    page: str,
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """未知单段路径一律 404（/me 已交付为真实页面，占位配置已清空）。"""
    raise HTTPException(status_code=404)
