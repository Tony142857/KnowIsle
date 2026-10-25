"""审核流水线任务（模块 B2）：投稿自动预检。

预检异常由 arq 自动重试（max_tries=3，见 workers/settings.py）；
末次重试仍失败则容错推进到人工协审（precheck_result 标记 error，
交由协审员人工判断），避免投稿卡死在预检阶段。
"""

import logging

from app.moderation.precheck import run_precheck
from app.moderation.workflow import apply_precheck_result
from app.storage.db import SessionLocal
from app.storage.models import Resource, ReviewTask

logger = logging.getLogger(__name__)


async def precheck_submission(ctx: dict, task_id: int) -> None:
    """ARQ 任务：自动预检 → 应用结果（硬失败直接驳回 / 推进协审并指派）。"""
    async with SessionLocal() as session:
        task = await session.get(
            ReviewTask, task_id, with_for_update=True, populate_existing=True
        )
        if task is None:
            logger.warning("预检跳过：审核任务不存在 task_id=%s", task_id)
            return
        if task.stage != "precheck":
            logger.info("预检跳过：任务已推进 task_id=%s stage=%s", task_id, task.stage)
            return
        resource = await session.get(Resource, task.resource_id)
        if resource is None:
            logger.warning("预检跳过：资源不存在 task_id=%s", task_id)
            return
        try:
            result = await run_precheck(session, resource)
            if task.stage == "precheck":
                await apply_precheck_result(session, task, result)
                await session.commit()
        except Exception:
            await session.rollback()
            if ctx.get("job_try", 1) >= 3:
                task = await session.get(
                    ReviewTask, task_id, with_for_update=True, populate_existing=True
                )
                if task is not None and task.stage == "precheck":
                    fallback = {
                        "format_ok": None,
                        "duplicate": None,
                        "sensitive_hits": [],
                        "ai_review": {},
                        "hard_fail": False,
                        "reason": "",
                        "error": "预检异常，转人工协审",
                    }
                    await apply_precheck_result(session, task, fallback)
                    await session.commit()
            raise
    logger.info("投稿预检完成 task_id=%s", task_id)
