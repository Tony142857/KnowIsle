"""AI 首答生成任务（模块 B3）：问答贴发帖后异步生成 AI 参考回答。

以「标题 + 正文前 500 字」为 query 检索本课程公共库（pipeline.retrieve），
按 pipeline.py 的格式组装上下文，SHORT 档非流式生成；成功写 posts.ai_first_answer
并通知提问者，失败由 ARQ 重试（全局 max_tries=3），末次重试置 Redis failed 状态。
"""

import logging

from app.community.posts import AI_ANSWER_KEY_TTL, ai_answer_key
from app.core.llm.prompts import AI_FIRST_ANSWER_PROMPT
from app.core.llm.router import ModelTier, get_official_client
from app.core.pipeline import retrieve
from app.storage.cache import get_redis
from app.storage.db import SessionLocal
from app.storage.models import Notification, Post, User

logger = logging.getLogger(__name__)

_QUERY_CONTENT_LIMIT = 500  # 检索 query / 提问摘要取正文开头长度

_MAX_TRIES = 3  # 与 WorkerSettings.max_tries 对齐


async def generate_ai_first_answer(ctx: dict, post_id: int) -> None:
    """ARQ 任务：为问答贴生成 AI 首答（无命中也生成，由提示词说明资料不足）。"""
    redis = get_redis()
    key = ai_answer_key(post_id)
    async with SessionLocal() as session:
        post = await session.get(Post, post_id)
        if post is None or post.board != "qa" or post.ai_first_answer:
            await redis.delete(key)  # 早退路径清理 pending 键，避免残留至 TTL 过期
            return
        try:
            author = await session.get(User, post.author_id)
            question = f"{post.title}\n{post.content[:_QUERY_CONTENT_LIMIT]}"
            hits, _ = await retrieve(session, question, author, post.course_id, "public")
            context = "\n\n".join(
                f"[{h['chunk'].chunk_id}]\n{h['chunk'].content}" for h in hits
            )
            prompt = AI_FIRST_ANSWER_PROMPT.format(
                context=context or "（未检索到相关资料）", question=question
            )
            answer, _ = await get_official_client(ModelTier.SHORT).chat(
                [{"role": "user", "content": prompt}]
            )
            post.ai_first_answer = answer
            session.add(
                Notification(
                    user_id=post.author_id,
                    type="ai_answer",
                    title="你的提问有了 AI 参考回答",
                    link=f"/posts/{post_id}",
                )
            )
            await session.commit()
            await redis.delete(key)
            logger.info("AI 首答生成完成 post_id=%s", post_id)
        except Exception:
            await session.rollback()
            if ctx.get("job_try", 1) >= _MAX_TRIES:
                logger.exception("AI 首答生成失败（末次重试）post_id=%s", post_id)
                await redis.set(key, "failed", ex=AI_ANSWER_KEY_TTL)
                return
            raise
