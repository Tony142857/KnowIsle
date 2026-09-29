"""协审超时自动重指派（模块 B2，v0.5）：协审超时的任务重指派 / 直送终审。

由 ARQ cron 定时扫描（见 workers/settings.py）：stage='co_review' 且进入协审
超过 settings.review_co_timeout_hours 的任务，未提交意见的旧指派视为超时，
重新指派补足到 2 人（已提交意见者保留）；找不到新协审员且无已提交意见时，
按 v0.4 既定兜底策略直送管理员终审（与 workflow.apply_precheck_result 一致）。
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.moderation.assign import assign_reviewers
from app.storage.db import SessionLocal
from app.storage.models import Notification, Resource, ReviewRecord, ReviewTask, User

logger = logging.getLogger(__name__)

CO_REVIEW_NEEDED = 2  # 协审目标人数


def co_review_deadline(now: datetime, timeout_hours: int) -> datetime:
    """协审超时截止时间（纯函数）：created_at 早于该时间即为超时。"""
    return now - timedelta(hours=timeout_hours)


def is_co_overdue(created_at: datetime, now: datetime, timeout_hours: int) -> bool:
    """任务是否协审超时（纯函数）。"""
    return created_at < co_review_deadline(now, timeout_hours)


def merge_assignees(
    old_ids: list[int] | None,
    voted_ids: list[int],
    new_ids: list[int],
    needed: int = CO_REVIEW_NEEDED,
) -> list[int]:
    """合并协审指派（纯函数）：已提交意见者按原顺序保留，追加新指派者
    （去重、排除已提交者），总量至多 needed。"""
    voted = set(voted_ids)
    merged = [uid for uid in (old_ids or []) if uid in voted]
    for uid in new_ids:
        if len(merged) >= needed:
            break
        if uid not in merged and uid not in voted:
            merged.append(uid)
    return merged


def _notify_assign(session, user_id: int, resource: Resource, reason: str) -> None:
    """协审指派通知：type 与 workflow.apply_precheck_result 的指派通知一致。"""
    session.add(
        Notification(
            user_id=user_id,
            type="review_assign",
            title=f"新协审任务：{resource.title}",
            body=reason,
            link="/review",
        )
    )


async def _voted_reviewer_ids(session, task_id: int) -> list[int]:
    """已提交协审意见的 reviewer_id 列表。"""
    return list(
        (
            await session.execute(
                select(ReviewRecord.reviewer_id).where(
                    ReviewRecord.task_id == task_id,
                    ReviewRecord.stage == "co_review",
                )
            )
        ).scalars().all()
    )


async def _escalate_to_final(session, task: ReviewTask, resource: Resource) -> None:
    """兜底：无可用协审员时直送管理员终审，并通知全体在职管理员。"""
    task.stage = "final"
    admins = (
        await session.execute(
            select(User).where(User.role == "admin", User.status == "active")
        )
    ).scalars().all()
    for admin in admins:
        session.add(
            Notification(
                user_id=admin.id,
                type="review_escalate",
                title=f"协审无人可指派，直送终审：{resource.title}",
                body="协审超时且无可重新指派的协审员，请管理员终审",
                link="/admin/review",
            )
        )


async def review_timeout_scan(ctx: dict) -> dict:
    """ARQ cron：扫描协审超时任务，超时未提交意见的指派自动换人。

    重指派后 created_at 重置为当前时间，为新协审员重新计时；
    全部处理完一次 commit。返回 {"rescanned", "reassigned", "escalated"}。
    """
    now = datetime.now(UTC)
    deadline = co_review_deadline(now, get_settings().review_co_timeout_hours)
    reassigned = 0
    escalated = 0
    async with SessionLocal() as session:
        tasks = (
            await session.execute(
                select(ReviewTask).where(
                    ReviewTask.stage == "co_review",
                    ReviewTask.created_at < deadline,
                )
            )
        ).scalars().all()
        for task in tasks:
            voted_ids = await _voted_reviewer_ids(session, task.id)
            old_ids = task.assignee_ids or []
            timed_out = [uid for uid in old_ids if uid not in set(voted_ids)]
            if not timed_out:
                # 无超时指派（理论上全部提交后状态机已推进），跳过
                continue
            resource = await session.get(Resource, task.resource_id)
            keep = [uid for uid in old_ids if uid in set(voted_ids)]
            needed = CO_REVIEW_NEEDED - len(keep)
            new_ids = (
                # 排除全部原指派者（含超时未响应者），避免小协审员池下任务落回同一人
                await assign_reviewers(session, resource, count=needed, exclude_ids=old_ids)
                if needed > 0
                else []
            )
            if not new_ids and not keep:
                await _escalate_to_final(session, task, resource)
                escalated += 1
                continue
            task.assignee_ids = merge_assignees(old_ids, voted_ids, new_ids)
            task.created_at = now  # 重指派后重新计时
            for user_id in new_ids:
                _notify_assign(session, user_id, resource, "原协审超时未响应，已重新指派")
            reassigned += 1
        await session.commit()
    result = {"rescanned": len(tasks), "reassigned": reassigned, "escalated": escalated}
    logger.info("协审超时扫描完成 %s", result)
    return result
