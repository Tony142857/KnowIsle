"""经验帖 AI 摘要生成任务（模块 B3，v0.7）：经验长廊发帖后异步生成摘要。

以「标题 + 正文前 2000 字」为输入，按 §11.7 提示词走官方模型 SHORT 档非流式生成；
成功写 posts.ai_summary 并通知作者，失败由 ARQ 重试（全局 max_tries=3），
末次重试置 Redis failed 状态（详情页据此展示失败提示）。
"""

import logging

from app.community.posts import SUMMARY_KEY_TTL, exp_summary_key
from app.core.llm.prompts import EXPERIENCE_SUMMARY_PROMPT
from app.core.llm.router import ModelTier, get_official_client
from app.storage.cache import get_redis
from app.storage.db import SessionLocal
from app.storage.models import Notification, Post

logger = logging.getLogger(__name__)

_CONTENT_LIMIT = 2000  # 摘要输入取正文前 2000 字（§11.7 要求 100 字内摘要，输入截断控成本）

_MAX_TRIES = 3  # 与 WorkerSettings.max_tries 对齐


async def generate_experience_summary(ctx: dict, post_id: int) -> None:
    """ARQ 任务：为经验长廊帖生成 AI 摘要（LLM 异常按重试策略交给 ARQ）。"""
    redis = get_redis()
    key = exp_summary_key(post_id)
    async with SessionLocal() as session:
        post = await session.get(Post, post_id)
        if post is None or post.board != "experience" or post.ai_summary:
            # 早退路径清理待办键，避免详情页长期停留 pending 轮询
            await redis.delete(key)
            return
        try:
            content = f"{post.title}\n{post.content[:_CONTENT_LIMIT]}"
            prompt = EXPERIENCE_SUMMARY_PROMPT.format(post_content=content)
            summary, _ = await get_official_client(ModelTier.SHORT).chat(
                [{"role": "user", "content": prompt}]
            )
            post.ai_summary = summary
            session.add(
                Notification(
                    user_id=post.author_id,
                    type="ai_summary",
                    title="你的经验帖已生成 AI 摘要",
                    link=f"/posts/{post_id}",
                )
            )
            await session.commit()
            await redis.delete(key)
            logger.info("经验帖 AI 摘要生成完成 post_id=%s", post_id)
        except Exception:
            await session.rollback()
            if ctx.get("job_try", 1) >= _MAX_TRIES:
                logger.exception("经验帖 AI 摘要生成失败（末次重试）post_id=%s", post_id)
                await redis.set(key, "failed", ex=SUMMARY_KEY_TTL)
                return
            raise
