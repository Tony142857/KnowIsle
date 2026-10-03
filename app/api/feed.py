"""首页动态流接口（§13，v0.9）：GET /api/feed。

语义约定（匿名友好，不返回 401）：
- hot_posts 对所有人开放：全站近 7 天热帖（status ∈ normal/featured），
  先取最新 50 条候选，再按（投票分, 发布时间）倒序取前 10——即
  「近 7 天优先 + vote_score 倒序」的组合排序。
- new_resources 仅登录用户有数据：本人关注课程（follows.target_type=course）
  最新上架的资源（approved，按上架时间倒序，至多 10 条）；匿名或未关注任何课程
  时恒为空数组——首页据此渲染「登录引导卡 / 关注引导态」，两条路径共用同一
  响应结构，前端无需区分 401。
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.rbac import get_current_user_optional
from app.storage.db import get_db
from app.storage.models import Comment, Course, Follow, Post, Resource, User, Vote

router = APIRouter(prefix="/feed", tags=["feed"])

FEED_SIZE = 10
HOT_WINDOW_DAYS = 7
HOT_CANDIDATES = 50

# 板块展示名（与 pages.BOARDS 同源；API 层不反向依赖页面层，故单独声明）
BOARD_NAMES = {
    "qa": "知屿问答",
    "discuss": "讨论区",
    "experience": "经验长廊",
    "bounty": "资料求援",
}


async def _new_resources(db: AsyncSession, user: User) -> list[dict]:
    """本人关注课程的最新上架资源；未关注任何课程时不查资源表，直接返回空。"""
    course_ids = (
        await db.execute(
            select(Follow.target_id).where(
                Follow.user_id == user.id, Follow.target_type == "course"
            )
        )
    ).scalars().all()
    if not course_ids:
        return []
    rows = (
        await db.execute(
            select(Resource, Course.name)
            .join(Course, Course.id == Resource.course_id)
            .where(
                Resource.review_status == "approved",
                Resource.course_id.in_(course_ids),
            )
            .order_by(Resource.created_at.desc(), Resource.id.desc())
            .limit(FEED_SIZE)
        )
    ).all()
    return [
        {
            "id": r.id,
            "title": r.title,
            "course_id": r.course_id,
            "course_name": course_name,
            "created_at": r.created_at,
            "url": f"/resources/{r.id}",
        }
        for r, course_name in rows
    ]


async def _hot_posts(db: AsyncSession) -> list[dict]:
    """近 7 天热帖：候选 + 评论数/投票分 IN 批量聚合（消 N+1，同板块列表页写法）。"""
    cutoff = datetime.now(UTC) - timedelta(days=HOT_WINDOW_DAYS)
    posts = (
        await db.execute(
            select(Post)
            .where(Post.status.in_(["normal", "featured"]), Post.created_at >= cutoff)
            .order_by(Post.created_at.desc())
            .limit(HOT_CANDIDATES)
        )
    ).scalars().all()
    if not posts:
        return []
    post_ids = [p.id for p in posts]
    comment_counts = dict(
        (
            await db.execute(
                select(Comment.post_id, func.count())
                .where(Comment.post_id.in_(post_ids))
                .group_by(Comment.post_id)
            )
        ).all()
    )
    scores = dict(
        (
            await db.execute(
                select(Vote.target_id, func.sum(Vote.value))
                .where(Vote.target_type == "post", Vote.target_id.in_(post_ids))
                .group_by(Vote.target_id)
            )
        ).all()
    )
    ranked = sorted(
        posts,
        key=lambda p: (scores.get(p.id, 0) or 0, p.created_at),
        reverse=True,
    )[:FEED_SIZE]
    return [
        {
            "id": p.id,
            "title": p.title,
            "board": p.board,
            "board_name": BOARD_NAMES.get(p.board, p.board),
            "score": scores.get(p.id, 0) or 0,
            "comment_count": comment_counts.get(p.id, 0),
            "created_at": p.created_at,
            "url": f"/posts/{p.id}",
        }
        for p in ranked
    ]


@router.get("")
async def get_feed(
    user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """首页动态流：登录用户返回「关注课程新资料 + 全站热帖」，匿名仅返回热帖。"""
    new_resources = await _new_resources(db, user) if user is not None else []
    return {"new_resources": new_resources, "hot_posts": await _hot_posts(db)}
