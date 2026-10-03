"""成长激励（模块 B4 / §3.3）：贡献分、等级、信用分三线体系。

贡献分事件驱动结算：统一写 score_logs 明细 + users.score 事务更新 +
等级阈值检查 + Redis 贡献榜（rank:major:{id} / rank:course:{id}）Sorted Set 实时排名。
信用分：管理员裁决扣分（credit_logs 留痕 + 通知本人，v0.6）；
v0.8 起裁决后自动执行阶梯处罚（<80 门控限流 / <60 禁言 / <40 冻结）。

v0.5 落地：grant_score 升级判定与站内通知、Redis 实时榜、下载分成纯函数、
课程贡献 SQL 现算（榜单退化与 settle_worker 每日对账共用）。
v0.6 落地：apply_credit_change 信用裁决（CreditLog 留痕 + 限幅 0~100 + 通知）。
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.platform_config import get_config
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
# v0.8：裁决后自动执行阶梯处罚（<80 限流门控 / <60 禁言 / <40 冻结）
# ---------------------------------------------------------------------------

CREDIT_MIN = 0
CREDIT_MAX = 100  # 信用分上下限（初始 100）

TIER_RATE_LIMIT = 80  # 低于此分：发帖/评论/上传走 Redis 冷却（门控实时判定，不落库）
TIER_MUTE = 60  # 低于此分：自动禁言 credit_mute_days 天
TIER_FREEZE = 40  # 低于此分：自动冻结


def clamp_credit(value: int) -> int:
    """信用分限幅（纯函数）：裁决后分值钳制在 [CREDIT_MIN, CREDIT_MAX]。"""
    return max(CREDIT_MIN, min(CREDIT_MAX, value))


def evaluate_credit_tier(credit: int) -> str:
    """信用阶梯评估（纯函数）：frozen / muted / rate_limited / normal。"""
    if credit < TIER_FREEZE:
        return "frozen"
    if credit < TIER_MUTE:
        return "muted"
    if credit < TIER_RATE_LIMIT:
        return "rate_limited"
    return "normal"


def effective_status(status: str, muted_until: datetime | None, now: datetime) -> str:
    """有效状态推导（纯函数）：muted 且 muted_until 已到期视为 active（惰性解除由门控落库）。"""
    if status == "muted" and muted_until is not None and muted_until <= now:
        return "active"
    return status


async def _enforce_credit_tier(session: AsyncSession, user: User, now: datetime) -> None:
    """按裁决后信用分自动落库阶梯处罚；状态变化（含禁言期限重置）时追加通知。

    rate_limited 档不落库（users.status 无对应取值），由 identity.penalty 门控
    按 credit 实时判定冷却。
    """
    tier = evaluate_credit_tier(user.credit)
    if tier == "frozen":
        if user.status != "frozen":
            user.status = "frozen"
            user.muted_until = None
            session.add(
                Notification(
                    user_id=user.id,
                    type="penalty",
                    title="账号已冻结",
                    body=f"信用分降至 {user.credit}（<40），账号已冻结，请联系管理员申诉",
                    link="/me",
                )
            )
    elif tier == "muted":
        mute_days = await get_config(session, "credit_mute_days")
        until = now + timedelta(days=mute_days)
        user.status = "muted"
        user.muted_until = until  # 已在禁言期则重置为全新周期
        session.add(
            Notification(
                user_id=user.id,
                type="penalty",
                title=f"账号禁言 {mute_days} 天",
                body=f"信用分降至 {user.credit}（<60），禁言至 {until.isoformat()}，期间无法发帖与评论",
                link="/me",
            )
        )
    else:  # normal / rate_limited：解除既有的禁言或冻结
        if user.status == "muted":
            user.status = "active"
            user.muted_until = None
            session.add(
                Notification(
                    user_id=user.id,
                    type="credit_restore",
                    title="禁言已解除",
                    body=f"信用分恢复至 {user.credit}，禁言已解除",
                    link="/me",
                )
            )
        elif user.status == "frozen":
            user.status = "active"
            user.muted_until = None
            session.add(
                Notification(
                    user_id=user.id,
                    type="credit_restore",
                    title="冻结已解除",
                    body=f"信用分恢复至 {user.credit}，账号已解冻",
                    link="/me",
                )
            )


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
    通知本人（扣分 penalty / 恢复 credit_restore）+ 自动执行阶梯处罚（v0.8，
    状态实际变化时追加 penalty/credit_restore 通知）。调用方负责 commit 与审计。
    返回裁决后信用分。
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
    await _enforce_credit_tier(session, user, datetime.now(UTC))
    return user.credit
