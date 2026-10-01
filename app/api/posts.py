"""社区帖子接口（§12.1）：帖子列表/发帖、详情（含 AI 首答/AI 摘要）、评论、采纳。

v0.7：经验长廊发帖入队 AI 摘要异步生成（§11.7 提示词，写入 posts.ai_summary）；
资料求援发帖托管悬赏分（bounty_escrow），采纳响应评论时赏金结算给响应者
（bounty_award；不可采纳自己的响应，结算后不可改采），响应评论即通知帖主
（§B5 悬赏被响应事件源）。
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.community.bounty import (
    SCORE_BOUNTY_AWARD,
    SCORE_BOUNTY_ESCROW,
    decide_bounty_award,
    validate_bounty_board,
    validate_bounty_score,
)
from app.community.comments import decide_accept_score, validate_accept
from app.community.posts import (
    AI_ANSWER_KEY_TTL,
    SUMMARY_KEY_TTL,
    ai_answer_key,
    build_ai_citations,
    derive_ai_answer_status,
    derive_summary_status,
    exp_summary_key,
    make_excerpt,
    validate_board,
    validate_tags,
)
from app.identity.growth import grant_score
from app.identity.rbac import get_current_user, get_current_user_optional
from app.storage.cache import get_redis
from app.storage.db import get_db
from app.storage.models import Chapter, Comment, Course, Notification, Post, User, Vote
from app.workers.pool import get_arq_pool

router = APIRouter(prefix="/posts", tags=["posts"])

logger = logging.getLogger(__name__)

LIST_DEFAULT_SIZE = 20
LIST_MAX_SIZE = 100

def _author_brief(user: User) -> dict:
    return {"id": user.id, "nickname": user.nickname, "avatar_url": user.avatar_url}


# ---------------------------------------------------------------------------
# 发帖 / 列表 / 详情
# ---------------------------------------------------------------------------


class CreatePostRequest(BaseModel):
    board: str
    title: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1)
    course_id: int | None = None
    chapter_id: int | None = None
    tags: list[str] | None = None
    bounty_score: int = Field(default=0, ge=0)  # 仅资料求援板块：托管悬赏贡献分


@router.post("", status_code=201, include_in_schema=False)
@router.post("/", status_code=201)
async def create_post(
    payload: CreatePostRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """发帖：qa 帖必选公共课程并入队 AI 首答；experience 帖入队 AI 摘要；
    bounty 帖托管悬赏分；discuss 帖无附加动作。"""
    try:
        validate_board(payload.board)
        tags = validate_tags(payload.tags)
        validate_bounty_board(payload.board, payload.bounty_score)
        if payload.board == "bounty":
            validate_bounty_score(payload.bounty_score)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    course = None
    if payload.board == "qa" and payload.course_id is None:
        raise HTTPException(status_code=422, detail="问答贴必须指定课程")
    if payload.course_id is not None:
        course = await db.get(Course, payload.course_id)
        if course is None or course.scope != "public" or course.status != "active":
            raise HTTPException(status_code=422, detail="课程不存在或未开放")
    if payload.chapter_id is not None:
        if course is None:
            raise HTTPException(status_code=422, detail="指定章节须同时指定课程")
        chapter = await db.get(Chapter, payload.chapter_id)
        if chapter is None or chapter.course_id != course.id:
            raise HTTPException(status_code=422, detail="章节不存在或不属于该课程")
    if payload.board == "bounty":
        # 行锁串行化并发发帖的托管扣分判定，防并发透支（H2）；
        # user 已被 get_current_user 加载进身份映射，必须 populate_existing=True
        # 强制发出 SELECT ... FOR UPDATE，否则 session.get 直接命中缓存、行锁不生效；
        # grant_score 经 session.get(User, ...) 复用同一身份映射实例，锁持续有效
        locked_user = await db.get(
            User, user.id, with_for_update=True, populate_existing=True
        )
        if locked_user.score < payload.bounty_score:
            raise HTTPException(status_code=422, detail="贡献分不足，无法托管悬赏")

    post = Post(
        author_id=user.id,
        board=payload.board,
        major_id=course.major_id if course else None,
        course_id=course.id if course else None,
        chapter_id=payload.chapter_id,
        title=payload.title,
        content=payload.content,
        tags=tags or None,
        bounty_score=payload.bounty_score if payload.board == "bounty" else 0,
    )
    db.add(post)
    await db.flush()
    if post.board == "bounty":
        # 悬赏托管：帖主贡献分扣减（上行已持该行锁），采纳结算时转给响应者（v0.7 落地形态）
        await grant_score(
            db, user.id, -post.bounty_score, SCORE_BOUNTY_ESCROW, "post", post.id
        )
    await db.commit()

    ai_pending = False
    summary_pending = False
    redis = get_redis()
    if post.board == "qa":
        await redis.set(ai_answer_key(post.id), "pending", ex=AI_ANSWER_KEY_TTL)
        pool = await get_arq_pool()
        try:
            await pool.enqueue_job("generate_ai_first_answer", post.id)
        except Exception as exc:
            # 入队失败补偿（L3）：帖子已 commit，删除待办键并如实上报 500
            await redis.delete(ai_answer_key(post.id))
            logger.exception("AI 首答任务入队失败 post_id=%s", post.id)
            raise HTTPException(
                status_code=500, detail="AI 首答任务入队失败，请稍后重试"
            ) from exc
        ai_pending = True
    elif post.board == "experience":
        await redis.set(exp_summary_key(post.id), "pending", ex=SUMMARY_KEY_TTL)
        pool = await get_arq_pool()
        try:
            await pool.enqueue_job("generate_experience_summary", post.id)
        except Exception as exc:
            # 入队失败补偿（L3）：帖子已 commit，删除待办键并如实上报 500
            await redis.delete(exp_summary_key(post.id))
            logger.exception("经验帖 AI 摘要任务入队失败 post_id=%s", post.id)
            raise HTTPException(
                status_code=500, detail="AI 摘要任务入队失败，请稍后重试"
            ) from exc
        summary_pending = True
    return {
        "post_id": post.id,
        "ai_first_answer_pending": ai_pending,
        "ai_summary_pending": summary_pending,
    }


@router.get("")
@router.get("/")
async def list_posts(
    db: Annotated[AsyncSession, Depends(get_db)],
    board: str | None = None,
    course_id: int | None = None,
    tag: str | None = None,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=LIST_DEFAULT_SIZE, ge=1, le=LIST_MAX_SIZE),
):
    """帖子列表：仅 normal/featured，按发帖时间倒序；board 为空列全部。"""
    comment_counts = (
        select(Comment.post_id, func.count().label("n"))
        .group_by(Comment.post_id)
        .subquery()
    )
    vote_scores = (
        select(Vote.target_id, func.sum(Vote.value).label("s"))
        .where(Vote.target_type == "post")
        .group_by(Vote.target_id)
        .subquery()
    )
    filters = [Post.status.in_(["normal", "featured"])]
    if board:
        filters.append(Post.board == board)
    if course_id is not None:
        filters.append(Post.course_id == course_id)
    if tag:
        filters.append(Post.tags.contains([tag]))

    total = (
        await db.execute(select(func.count()).select_from(Post).where(*filters))
    ).scalar_one()
    rows = (
        await db.execute(
            select(Post, User, comment_counts.c.n, vote_scores.c.s)
            .join(User, User.id == Post.author_id)
            .outerjoin(comment_counts, comment_counts.c.post_id == Post.id)
            .outerjoin(vote_scores, vote_scores.c.target_id == Post.id)
            .where(*filters)
            .order_by(Post.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()
    # 经验帖且无摘要：一次 mget 批量取摘要状态键（避免逐条 N+1），其余帖子状态为 None
    summary_pending_posts = [
        post
        for post, *_ in rows
        if post.board == "experience" and not post.ai_summary
    ]
    summary_states: dict[int, str | None] = {}
    if summary_pending_posts:
        states = await get_redis().mget(
            [exp_summary_key(post.id) for post in summary_pending_posts]
        )
        summary_states = dict(
            zip((post.id for post in summary_pending_posts), states, strict=True)
        )
    items = [
        {
            "id": post.id,
            "board": post.board,
            "title": post.title,
            "author": _author_brief(author),
            "course_id": post.course_id,
            "tags": post.tags or [],
            "view_count": post.view_count,
            "comment_count": comment_count or 0,
            "vote_score": vote_score or 0,
            "status": post.status,
            "bounty_score": post.bounty_score,
            "ai_summary": post.ai_summary,
            "ai_summary_status": derive_summary_status(
                post.board, post.ai_summary, summary_states.get(post.id)
            ),
            "has_ai_answer": post.ai_first_answer is not None,
            "has_accepted": post.accepted_comment_id is not None,
            "created_at": post.created_at.isoformat() if post.created_at else None,
            "excerpt": make_excerpt(post.content),
        }
        for post, author, comment_count, vote_score in rows
    ]
    return {"total": total, "items": items}


@router.get("/{post_id}")
async def get_post(
    post_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """帖子详情：每次访问 view_count+1；含 AI 首答、状态推导与引用溯源。"""
    post = await db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Not Found")
    post.view_count += 1
    await db.commit()

    author = await db.get(User, post.author_id)
    vote_score = (
        await db.execute(
            select(func.coalesce(func.sum(Vote.value), 0)).where(
                Vote.target_type == "post", Vote.target_id == post.id
            )
        )
    ).scalar_one()
    my_vote = 0
    if user is not None:
        vote = (
            await db.execute(
                select(Vote.value).where(
                    Vote.user_id == user.id,
                    Vote.target_type == "post",
                    Vote.target_id == post.id,
                )
            )
        ).scalar_one_or_none()
        my_vote = vote or 0

    redis_state = None
    if post.board == "qa" and not post.ai_first_answer:
        redis_state = await get_redis().get(ai_answer_key(post.id))
    ai_citations = (
        await build_ai_citations(db, post.ai_first_answer) if post.ai_first_answer else []
    )
    summary_state = None
    if post.board == "experience" and not post.ai_summary:
        summary_state = await get_redis().get(exp_summary_key(post.id))
    return {
        "id": post.id,
        "board": post.board,
        "title": post.title,
        "content": post.content,
        "author": _author_brief(author),
        "course_id": post.course_id,
        "chapter_id": post.chapter_id,
        "tags": post.tags or [],
        "view_count": post.view_count,
        "vote_score": vote_score,
        "my_vote": my_vote,
        "status": post.status,
        "bounty_score": post.bounty_score,
        "ai_first_answer": post.ai_first_answer,
        "ai_answer_status": derive_ai_answer_status(
            post.board, post.ai_first_answer, redis_state
        ),
        "ai_citations": ai_citations,
        "ai_summary": post.ai_summary,
        "ai_summary_status": derive_summary_status(post.board, post.ai_summary, summary_state),
        "accepted_comment_id": post.accepted_comment_id,
        "created_at": post.created_at.isoformat() if post.created_at else None,
    }


# ---------------------------------------------------------------------------
# 评论 / 采纳
# ---------------------------------------------------------------------------


@router.get("/{post_id}/comments")
async def list_comments(
    post_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """评论平铺列表（按时间升序，前端组树），含点赞总分与我的投票。"""
    post = await db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Not Found")
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
    return {
        "items": [
            {
                "id": c.id,
                "parent_id": c.parent_id,
                "author": _author_brief(author),
                "content": c.content,
                "is_accepted": c.is_accepted,
                "vote_score": scores.get(c.id, 0),
                "my_vote": my_votes.get(c.id, 0),
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c, author in rows
        ]
    }


class CreateCommentRequest(BaseModel):
    content: str = Field(min_length=1)
    parent_id: int | None = None


@router.post("/{post_id}/comments", status_code=201)
async def create_comment(
    post_id: int,
    payload: CreateCommentRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """发表评论：支持楼中楼（parent_id 须属同一帖子）；closed 帖禁止评论。"""
    post = await db.get(Post, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Not Found")
    if post.status == "closed":
        raise HTTPException(status_code=422, detail="帖子已关闭")
    if payload.parent_id is not None:
        parent = await db.get(Comment, payload.parent_id)
        if parent is None or parent.post_id != post.id:
            raise HTTPException(status_code=422, detail="父评论不存在或不属于该帖子")
    comment = Comment(
        post_id=post.id, author_id=user.id, parent_id=payload.parent_id, content=payload.content
    )
    db.add(comment)
    await db.flush()
    if post.board == "bounty" and user.id != post.author_id:
        # 悬赏被响应（§B5 事件源，v0.7）：响应评论即通知帖主（楼中楼回复不重复通知）
        if payload.parent_id is None:
            db.add(
                Notification(
                    user_id=post.author_id,
                    type="bounty",
                    title=f"你的求援有了新响应：{post.title}",
                    link=f"/posts/{post.id}",
                )
            )
    await db.commit()
    return {"comment_id": comment.id}


@router.post("/comments/{comment_id}/accept")
async def accept_comment(
    comment_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """采纳：qa 帖采纳最佳答案（+15，自问自答不计分）；bounty 帖采纳响应即结算悬赏
    （托管赏金转给响应者；不可采纳自己的响应，结算后不可改采）；重复采纳同一条幂等，
    qa 帖改采只换标记不动积分。"""
    comment = await db.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="Not Found")
    # 行锁串行化同一帖的并发采纳，防并发重复结算（H1）
    post = await db.get(Post, comment.post_id, with_for_update=True)
    # 已采纳且非本评论：求援帖据此拦截结算后改采；重复采纳同一条的幂等分支不受影响
    already_accepted = (
        post.accepted_comment_id is not None and post.accepted_comment_id != comment.id
    )
    error = validate_accept(
        post.board, post.author_id, user.id, comment.author_id, already_accepted
    )
    if error == "not_owner":
        raise HTTPException(status_code=404, detail="Not Found")
    if error is not None:
        detail = {
            "not_board": "仅问答贴或求援贴可采纳",
            "self_response": "不能采纳自己的响应",
            "already_settled": "求援已结算，不可改采",
        }[error]
        raise HTTPException(status_code=422, detail=detail)

    if post.accepted_comment_id == comment.id and comment.is_accepted:
        return {"accepted": True, "score_granted": 0}  # 重复采纳同一条：幂等

    score_granted = 0
    if post.accepted_comment_id is None:
        # 首次采纳
        comment.is_accepted = True
        post.accepted_comment_id = comment.id
        if post.board == "bounty":
            # 悬赏结算：托管赏金全额转给响应者（v0.7 落地形态）
            award = decide_bounty_award(post.author_id, comment.author_id, post.bounty_score)
            if award:
                await grant_score(
                    db, comment.author_id, award, SCORE_BOUNTY_AWARD, "post", post.id
                )
                score_granted = award
                db.add(
                    Notification(
                        user_id=comment.author_id,
                        type="bounty",
                        title=f"你的响应被采纳，获得悬赏 {award} 分",
                        link=f"/posts/{post.id}",
                    )
                )
                db.add(
                    Notification(
                        user_id=post.author_id,
                        type="bounty",
                        title=f"求援已结算：{post.title}",
                        body=f"悬赏 {award} 分已转给响应者",
                        link=f"/posts/{post.id}",
                    )
                )
        else:
            score_granted = decide_accept_score(post.author_id, comment.author_id)
            if score_granted:
                await grant_score(
                    db, comment.author_id, score_granted, "answer_accepted", "comment",
                    comment.id, course_id=post.course_id,
                )
            db.add(
                Notification(
                    user_id=comment.author_id,
                    type="accepted",
                    title="你的回答被采纳",
                    link=f"/posts/{post.id}",
                )
            )
    else:
        # 改采另一条评论（仅 qa 可达：求援已结算后改采已被 already_settled 拦截）：
        # 旧评论取消标记，积分不再变动
        old = await db.get(Comment, post.accepted_comment_id)
        if old is not None:
            old.is_accepted = False
        comment.is_accepted = True
        post.accepted_comment_id = comment.id
    await db.commit()
    return {"accepted": True, "score_granted": score_granted}
