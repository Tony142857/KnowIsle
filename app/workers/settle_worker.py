"""积分结算 / 榜单快照任务（模块 B4）。

Redis 贡献榜 Sorted Set（rank:major:{id} / rank:course:{id}）每日全量对账重建：
SQL（users.score / score_logs 现算）为权威数据，校正实时增量（ZADD/ZINCRBY）
可能产生的漂移与 Redis 故障期的丢写。贡献分事件的事务化结算见 identity/growth。
"""

import logging

from sqlalchemy import select

from app.identity.growth import compute_course_scores
from app.storage.cache import get_redis
from app.storage.db import SessionLocal
from app.storage.models import User

logger = logging.getLogger(__name__)


async def settle_scores(ctx: dict) -> dict:
    """每日对账（cron 03:47）：全量重建专业榜与课程榜，返回 {"majors", "courses"} 摘要。

    每个键先 DEL 再 ZADD 重建，避免残留已退榜成员；单键失败不中断整体对账。
    """
    async with SessionLocal() as session:
        major_rows = (
            await session.execute(
                select(User.major_id, User.id, User.score).where(
                    User.major_id.is_not(None), User.score > 0
                )
            )
        ).all()
        course_scores = await compute_course_scores(session)

    redis = get_redis()
    majors: dict[int, dict[str, int]] = {}
    for major_id, user_id, score in major_rows:
        majors.setdefault(major_id, {})[str(user_id)] = score

    rebuilt_majors = 0
    for major_id, members in majors.items():
        key = f"rank:major:{major_id}"
        try:
            await redis.delete(key)
            await redis.zadd(key, members)
            rebuilt_majors += 1
        except Exception:
            logger.warning("专业榜对账重建失败 major_id=%s", major_id, exc_info=True)

    rebuilt_courses = 0
    for course_id, user_scores in course_scores.items():
        members = {str(uid): total for uid, total in user_scores.items() if total > 0}
        if not members:
            continue
        key = f"rank:course:{course_id}"
        try:
            await redis.delete(key)
            await redis.zadd(key, members)
            rebuilt_courses += 1
        except Exception:
            logger.warning("课程榜对账重建失败 course_id=%s", course_id, exc_info=True)

    summary = {"majors": rebuilt_majors, "courses": rebuilt_courses}
    logger.info("贡献榜每日对账完成 %s", summary)
    return summary
