"""协审员指派（模块 B2）：按专业匹配 + 随机指派，48h 限时，超时重新指派。

协审质量纳入考核：协审结论与终审结论偏差率过高的协审员取消资格。
（48h 限时重指派与质量考核留 v0.5，当前仅实现候选选取。）
"""

import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import Course, Resource, User


async def assign_reviewers(
    session: AsyncSession, resource: Resource, count: int = 2
) -> list[int]:
    """候选：active 协审员（排除投稿人本人）；与目标课程同 major 的排前，
    其余随机，取前 count 个（不足则全返回）。"""
    course = await session.get(Course, resource.course_id)
    candidates = (
        await session.execute(
            select(User).where(
                User.role == "reviewer",
                User.status == "active",
                User.id != resource.uploader_id,
            )
        )
    ).scalars().all()
    same_major = [u.id for u in candidates if u.major_id == course.major_id]
    others = [u.id for u in candidates if u.major_id != course.major_id]
    random.shuffle(same_major)
    random.shuffle(others)
    return (same_major + others)[:count]
