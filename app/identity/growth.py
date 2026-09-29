"""成长激励（模块 B4 / §3.3）：贡献分、等级、信用分三线体系。

贡献分事件驱动结算：统一写 score_logs 明细 + users.score 事务更新 +
等级阈值检查 + Redis 贡献榜（rank:major:{id} / rank:course:{id}）Sorted Set 实时排名。
信用分：管理员裁决扣分（credit_logs 留痕），阈值触发限流/禁言/冻结阶梯处罚。

v0.4 落地 grant_score 简单版（明细 + 余额，同一事务由调用方 commit）；
等级升级与 Redis 榜单留 v0.5（见 workers/settle_worker）。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import ScoreLog, User

SCORE_UPLOAD_APPROVED = 20  # 投稿终审通过


async def grant_score(
    session: AsyncSession,
    user_id: int,
    delta: int,
    reason: str,
    ref_type: str | None = None,
    ref_id: int | None = None,
) -> None:
    """记一条 ScoreLog 并同步 User.score（调用方负责 commit，同事务保证一致）。"""
    session.add(
        ScoreLog(
            user_id=user_id, delta=delta, reason=reason, ref_type=ref_type, ref_id=ref_id
        )
    )
    user = await session.get(User, user_id)
    user.score += delta


# TODO(v0.5): maybe_level_up / apply_credit_penalty
