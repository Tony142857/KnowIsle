"""AI 首答生成任务（模块 B3）：问答贴发帖后异步生成 AI 参考回答。

以「标题 + 正文前 500 字」为 query 检索本课程公共库（pipeline.retrieve），
按 pipeline.py 的格式组装上下文，SHORT 档非流式生成；成功写 posts.ai_first_answer
并通知提问者。§20.5 B3（v1.1）：课程被禁用/删除等永久性错误（retrieve 抛
HTTPException）不再占用重试——直接置 Redis failed 状态早退；其余瞬时异常
由 ARQ 重试（全局 max_tries=3），末次重试置 Redis failed 状态。
"""

import logging

from fastapi import HTTPException

from app.community.posts import AI_ANSWER_KEY_TTL, ai_answer_key
from app.core.context import prompt_text
from app.core.llm.prompts import AI_FIRST_ANSWER_PROMPT
from app.core.llm.router import ModelTier, get_official_client
from app.core.pipeline import extract_citations, retrieve
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
        author = await session.get(User, post.author_id)
        if author is None:
            # 作者账号不存在（永久性）：置 failed 早退，不重试
            logger.warning("AI 首答永久失败：作者不存在 post_id=%s", post_id)
            await redis.set(key, "failed", ex=AI_ANSWER_KEY_TTL)
            return
        try:
            question = f"{post.title}\n{post.content[:_QUERY_CONTENT_LIMIT]}"
            hits, _ = await retrieve(session, question, author, post.course_id, "public")
            if getattr(hits, "metrics", {}).get("weak_evidence"):
                hits = []
            evidence_ids = {hit["chunk"].chunk_id for hit in hits}
            if hits:
                context = "\n\n".join(
                    prompt_text(
                        hit["chunk"], getattr(hit["chunk"], "_rag_chapter_title", None)
                    )
                    for hit in hits
                )
                prompt = AI_FIRST_ANSWER_PROMPT.format(context=context, question=question)
                answer, _ = await get_official_client(ModelTier.SHORT).chat(
                    [{"role": "user", "content": prompt}]
                )
            else:
                answer = (
                    "以下为 AI 基于社区资料的参考回答，欢迎同学补充纠正。"
                    "当前公共资料中未检索到足以回答该问题的内容，请补充具体章节或相关课件；"
                    "也欢迎了解该课程的同学补充实战经验。"
                )
            post.ai_first_answer = answer
            post.ai_citation_chunk_ids = extract_citations(answer, evidence_ids)
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
        except HTTPException as exc:
            # §20.5 B3：课程被禁用/越权等永久性错误，重试无意义 → 置 failed 早退
            await session.rollback()
            logger.warning(
                "AI 首答永久失败（不重试）post_id=%s detail=%s", post_id, exc.detail
            )
            await redis.set(key, "failed", ex=AI_ANSWER_KEY_TTL)
        except Exception:
            await session.rollback()
            if ctx.get("job_try", 1) >= _MAX_TRIES:
                logger.exception("AI 首答生成失败（末次重试）post_id=%s", post_id)
                await redis.set(key, "failed", ex=AI_ANSWER_KEY_TTL)
                return
            raise
