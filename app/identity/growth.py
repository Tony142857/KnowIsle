"""成长激励（模块 B4 / §3.3）：贡献分、等级、信用分三线体系。

贡献分事件驱动结算：统一写 score_logs 明细 + users.score 事务更新 +
等级阈值检查 + Redis 贡献榜（rank:major:{id} / rank:course:{id}）Sorted Set 实时排名。
信用分：管理员裁决扣分（credit_logs 留痕 + 通知本人，v0.6）；
阶梯处罚（限流/禁言/冻结自动执行）留 v0.8 与举报一起做。

v0.5 落地：grant_score 升级判定与站内通知、Redis 实时榜、下载分成纯函数、
课程贡献 SQL 现算（榜单退化与 settle_worker 每日对账共用）。
v0.6 落地：apply_credit_change 信用裁决（CreditLog 留痕 + 限幅 0~100 + 通知）。
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.cache import get_redis
from app.storage.models import (
    Comment,
    CreditLog,
    Notification,
    Post,
    Resource,
    ScoreLog,
    User,
)

logger = logging.getLogger(__name__)

SCORE_UPLOAD_APPROVED = 20  # 投稿终审通过

LEVEL_THRESHOLDS = (0, 50, 150, 400, 1000, 2500)  # Lv1~Lv6 所需最低贡献分

# 计入课程贡献榜的积分事件：资源类按 ref_id 归属课程，回答采纳经 comment→post 归属
COURSE_RESOURCE_REASONS = ("upload_approved", "download_share", "download_reward")
COURSE_COMMENT_REASONS = ("answer_accepted",)


def level_for_score(score: int) -> int:
    """按阈值表计算等级（1~6）；低于 Lv1 阈值按 Lv1 处理。"""
    level = 1
    for threshold in LEVEL_THRESHOLDS[1:]:
        if score < threshold:
            break
        level += 1
    return level


def download_share(cost: int) -> int:
    """下载付费积分上传者分成：80% 给上传者（向下取整），其余平台回收。"""
    return cost * 4 // 5


async def grant_score(
    session: AsyncSession,
    user_id: int,
    delta: int,
    reason: str,
    ref_type: str | None = None,
    ref_id: int | None = None,
    *,
    course_id: int | None = None,
) -> None:
    """记一条 ScoreLog 并同步 User.score（调用方负责 commit，同事务保证一致）。

    附带两项副作用：
    - 等级升级：更新后总分跨阈值则提升 user.level 并发 level_up 站内通知（只升不降）；
    - Redis 贡献榜实时更新：major 榜 ZADD 新总分，course 榜（传 course_id 时）
      ZINCRBY 增量；Redis 故障仅 log 告警，不影响计分主流程。
    """
    session.add(
        ScoreLog(
            user_id=user_id, delta=delta, reason=reason, ref_type=ref_type, ref_id=ref_id
        )
    )
    user = await session.get(User, user_id)
    user.score += delta

    new_level = level_for_score(user.score)
    if new_level > user.level:
        user.level = new_level
        session.add(
            Notification(
                user_id=user_id,
                type="level_up",
                title=f"恭喜升级到 Lv{new_level}",
                link="/me",
            )
        )

    try:
        redis = get_redis()
        if user.major_id is not None:
            await redis.zadd(f"rank:major:{user.major_id}", {str(user.id): user.score})
        if course_id is not None:
            await redis.zincrby(f"rank:course:{course_id}", delta, str(user.id))
    except Exception:
        logger.warning("贡献榜实时更新失败（不影响计分）user_id=%s", user_id, exc_info=True)


async def compute_course_scores(
    session: AsyncSession, course_id: int | None = None
) -> dict[int, dict[int, int]]:
    """从 score_logs 现算课程贡献：{course_id: {user_id: 总分}}。

    资源类事件（上传过审 / 下载分成 / 免费被下载奖励）经 ref_id→resources 归属课程；
    回答采纳经 ref_id→comments→posts 归属课程。course_id 为空时全量分组（对账用），
    否则只算单课程（榜单 Redis 缺失时的退化路径用）。
    """
    resource_q = (
        select(Resource.course_id, ScoreLog.user_id, func.sum(ScoreLog.delta))
        .join(Resource, Resource.id == ScoreLog.ref_id)
        .where(
            ScoreLog.ref_type == "resource",
            ScoreLog.reason.in_(COURSE_RESOURCE_REASONS),
        )
        .group_by(Resource.course_id, ScoreLog.user_id)
    )
    comment_q = (
        select(Post.course_id, ScoreLog.user_id, func.sum(ScoreLog.delta))
        .join(Comment, Comment.id == ScoreLog.ref_id)
        .join(Post, Post.id == Comment.post_id)
        .where(
            ScoreLog.ref_type == "comment",
            ScoreLog.reason.in_(COURSE_COMMENT_REASONS),
        )
        .group_by(Post.course_id, ScoreLog.user_id)
    )
    if course_id is not None:
        resource_q = resource_q.where(Resource.course_id == course_id)
        comment_q = comment_q.where(Post.course_id == course_id)

    scores: dict[int, dict[int, int]] = {}
    for query in (resource_q, comment_q):
        for cid, uid, total in await session.execute(query):
            if cid is None:
                continue
            course_scores = scores.setdefault(cid, {})
            course_scores[uid] = course_scores.get(uid, 0) + int(total)
    return scores


# ---------------------------------------------------------------------------
# 信用分（§3.3.3，v0.6）：管理员裁决扣分 / 恢复
# ---------------------------------------------------------------------------

CREDIT_MIN = 0
CREDIT_MAX = 100  # 信用分上下限（初始 100）


def clamp_credit(value: int) -> int:
    """信用分限幅（纯函数）：裁决后分值钳制在 [CREDIT_MIN, CREDIT_MAX]。"""
    return max(CREDIT_MIN, min(CREDIT_MAX, value))


async def apply_credit_change(
    session: AsyncSession,
    user: User,
    admin_id: int,
    delta: int,
    reason: str,
    ref_type: str | None = None,
    ref_id: int | None = None,
) -> int:
    """信用裁决：写 credit_logs（注明裁决人与理由）+ 限幅更新 users.credit +
    通知本人（扣分 penalty / 恢复 credit_restore）。调用方负责 commit 与审计。
    返回裁决后信用分。阶梯处罚自动执行留 v0.8。
    """
    session.add(
        CreditLog(
            user_id=user.id,
            admin_id=admin_id,
            delta=delta,
            reason=reason,
            ref_type=ref_type,
            ref_id=ref_id,
        )
    )
    user.credit = clamp_credit(user.credit + delta)
    if delta < 0:
        session.add(
            Notification(
                user_id=user.id,
                type="penalty",
                title=f"信用处罚：{delta} 分",
                body=reason,
                link="/me",
            )
        )
    else:
        session.add(
            Notification(
                user_id=user.id,
                type="credit_restore",
                title=f"信用恢复：+{delta} 分",
                body=reason,
                link="/me",
            )
        )
    return user.credit


# TODO(v0.8): 信用分阶梯处罚自动执行（<80 限流 / <60 禁言 7 天 / <40 冻结）
