"""协审员指派（模块 B2）：按专业匹配 + 随机指派，仅负责候选选取。

超时自动重指派（v0.5）见 workers/review_timeout_worker（其重指派复用本模块选人）。
协审质量纳入考核：协审结论与终审结论偏差率过高的协审员取消资格
（质量考核留后续版本，当前实现仅协审员候选选取）。
"""

import random
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import Course, Resource, User


async def assign_reviewers(
    session: AsyncSession,
    resource: Resource,
    count: int = 2,
    exclude_ids: Iterable[int] | None = None,
) -> list[int]:
    """候选：active 协审员（排除投稿人本人与 exclude_ids 指定的人）；
    与目标课程同 major 的排前，其余随机，取前 count 个（不足则全返回）。"""
    exclude = set(exclude_ids or ()) | {resource.uploader_id}
    course = await session.get(Course, resource.course_id)
    candidates = (
        await session.execute(
            select(User).where(
                User.role == "reviewer",
                User.status == "active",
                User.id.not_in(exclude),
            )
        )
    ).scalars().all()
    same_major = [u.id for u in candidates if u.major_id == course.major_id]
    others = [u.id for u in candidates if u.major_id != course.major_id]
    random.shuffle(same_major)
    random.shuffle(others)
    return (same_major + others)[:count]
