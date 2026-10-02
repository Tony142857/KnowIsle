"""RAG 复习工具接口（模块 A4，§12.1）：复习大纲 / 考点习题生成。

权限（§3.2）：个人课程仅本人可生成，公共课程（scope=public 且 active）任何登录用户
可生成，越权统一 404 不暴露存在性。官方通道先查 AI 日额度（429，§10.1），生成成功
后 qa_logs 落库（question 加 [review] 前缀与问答区分）并与额度扣减同事务提交。
LLM 故障统一 502；习题 JSON 解析失败重试一次，仍非法则 502。
串讲生成（/final）留二期，本模块不实现。
"""

import json
import logging
import re
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.prompts import QUIZ_PROMPT, REVIEW_OUTLINE_PROMPT
from app.core.llm.router import ModelTier, resolve_client
from app.core.pipeline import check_course_access, retrieve
from app.identity.quota import check_quota, consume_quota
from app.identity.rbac import get_current_user
from app.storage.db import get_db
from app.storage.models import Chapter, Course, QaLog, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/review", tags=["review-tools"])

_CHAPTER_CHUNK_LIMIT = 4  # 大纲：每章代表 chunks 上限
_COURSE_CHUNK_LIMIT = 8  # 大纲：课程无章节树时全课程代表 chunks 上限
_QUIZ_CHUNK_LIMIT = 6  # 习题：单章代表 chunks 上限
_QUIZ_TYPES = ("single_choice", "short_answer", "calculation")
_QUIZ_RETRY_HINT = "上次输出不是合法 JSON，请仅输出符合上述格式的 JSON，不要输出任何其他文字。"


class OutlineRequest(BaseModel):
    course_id: int
    chapter_ids: list[int] | None = Field(default=None, max_length=50)  # 缺省全课程


class QuizRequest(BaseModel):
    course_id: int
    chapter_id: int
    count: int = Field(default=5, ge=1, le=10)


async def _load_course(db: AsyncSession, user: User, course_id: int) -> Course:
    """课程存在性与数据级权限检查（个人课仅本人 / 公共课须 active，越权 404，§3.2）。"""
    course = await db.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Not Found")
    await check_course_access(db, user, course_id, course.scope)
    return course


async def _chapter_reps(
    db: AsyncSession, user: User, course: Course, chapter: Chapter, limit: int
) -> list[dict]:
    """章节代表 chunks：以章节标题为 query 走 §5.3 检索链路，过滤回本章；
    本章无命中时降级用全部命中（标题检索可能漏召回，代表块宁宽勿缺）。"""
    hits, _ = await retrieve(db, chapter.title, user, course.id, course.scope)
    own = [h for h in hits if h["chunk"].chapter_id == chapter.id]
    return (own or hits)[:limit]


def _sections_text(sections: list[dict]) -> str:
    """章节聚合文本：## 章节标题 + [chunk_id] 标注的内容块（供 Prompt {chapter_text}）。"""
    parts = []
    for sec in sections:
        blocks = [f"[{h['chunk'].chunk_id}]\n{h['chunk'].content}" for h in sec["chunks"]]
        parts.append(f"## {sec['title']}\n\n" + "\n\n".join(blocks))
    return "\n\n".join(parts)


async def _log_and_consume(
    db: AsyncSession,
    user: User,
    course: Course,
    *,
    question: str,
    prompt: str,
    answer: str,
    provider: str,
    token_usage: int,
    latency_ms: int,
    chapter_ids: list[int] | None,
    top_chunks: list[str],
) -> None:
    """qa_logs 落库 + 官方额度扣减（同事务；失败仅记日志回滚，不影响已生成结果返回）。"""
    try:
        db.add(
            QaLog(
                user_id=user.id,
                question=question,
                course_id=course.id,
                search_scope=course.scope,
                coarse_chapters={"chapter_ids": chapter_ids} if chapter_ids else None,
                top_chunks=top_chunks,
                prompt=prompt,
                answer=answer,
                model_provider=provider,
                token_usage=token_usage,
                latency_ms=latency_ms,
            )
        )
        if provider != "user_custom":  # 自定义 Key 不占官方额度（§10.1）
            await consume_quota(db, user)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("复习工具 qa_logs 落库 / 额度扣减失败")


# ---------------------------------------------------------------------------
# 复习大纲（模块 A4 一期：核心考点 / 易混概念 / 典型题型，MEDIUM 档）
# ---------------------------------------------------------------------------


@router.post("/outline")
async def generate_outline(
    body: OutlineRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """生成复习大纲：按章节聚合代表 chunks → REVIEW_OUTLINE_PROMPT → MEDIUM 档一次生成。"""
    started = time.monotonic()
    client, provider = await resolve_client(db, user, ModelTier.MEDIUM)
    if provider == "official":  # 自定义 Key 不占官方额度
        await check_quota(db, user)
    course = await _load_course(db, user, body.course_id)

    stmt = (
        select(Chapter)
        .where(Chapter.course_id == course.id)
        .order_by(Chapter.order_idx, Chapter.id)
    )
    if body.chapter_ids:
        stmt = stmt.where(Chapter.id.in_(body.chapter_ids))
    chapters = (await db.execute(stmt)).scalars().all()
    if body.chapter_ids and len({c.id for c in chapters}) != len(set(body.chapter_ids)):
        raise HTTPException(status_code=404, detail="Not Found")  # 章节不属于本课程

    try:
        if chapters:
            sections = [
                {
                    "id": ch.id,
                    "title": ch.title,
                    "chunks": await _chapter_reps(db, user, course, ch, _CHAPTER_CHUNK_LIMIT),
                }
                for ch in chapters
            ]
        else:  # 课程无章节树：以课程名为 query 全课程范围检索代表 chunks
            hits, _ = await retrieve(db, course.name, user, course.id, course.scope)
            sections = [{"id": None, "title": "全课程", "chunks": hits[:_COURSE_CHUNK_LIMIT]}]
    except HTTPException:
        raise
    except Exception:
        logger.exception("复习大纲检索失败 course_id=%s", course.id)
        raise HTTPException(status_code=502, detail="检索服务暂时不可用，请稍后重试") from None

    sections = [s for s in sections if s["chunks"]]  # 跳过无资料章节
    if not sections:
        raise HTTPException(status_code=422, detail="该课程暂无可用于生成大纲的资料")

    prompt = REVIEW_OUTLINE_PROMPT.format(chapter_text=_sections_text(sections))
    try:
        outline, usage = await client.chat([{"role": "user", "content": prompt}])
    except Exception:
        logger.exception("复习大纲 LLM 生成失败 course_id=%s", course.id)
        raise HTTPException(status_code=502, detail="AI 服务暂时不可用，请稍后重试") from None

    token_usage = (usage or {}).get("total_tokens", 0)
    latency_ms = int((time.monotonic() - started) * 1000)
    top_chunks = [h["chunk"].chunk_id for s in sections for h in s["chunks"]]
    chapter_ids = [s["id"] for s in sections if s["id"] is not None]
    await _log_and_consume(
        db, user, course,
        question=f"[review] 复习大纲：{course.name}",
        prompt=prompt, answer=outline, provider=provider,
        token_usage=token_usage, latency_ms=latency_ms,
        chapter_ids=chapter_ids, top_chunks=top_chunks,
    )
    return {
        "course_id": course.id,
        "chapters": [
            {"id": s["id"], "title": s["title"], "chunk_count": len(s["chunks"])}
            for s in sections
        ],
        "outline": outline,
        "provider": provider,
        "token_usage": token_usage,
        "latency_ms": latency_ms,
    }


# ---------------------------------------------------------------------------
# 考点习题（模块 A4 一期：单章出题，结构化 JSON，答案解析附关联 chunk_id）
# ---------------------------------------------------------------------------


def _parse_quiz(raw: str, valid_chunk_ids: set[str]) -> list[dict] | None:
    """解析习题 JSON：容错 markdown 代码块包裹；逐题规整字段，
    chunk_id 仅保留上下文真实存在的（与 §A3 引用提取同一约定）。非法输出返回 None。"""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    items = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return None
    questions = []
    for item in items:
        if not isinstance(item, dict) or not item.get("question"):
            continue
        options = item.get("options")
        chunk_id = item.get("chunk_id")
        questions.append(
            {
                "type": item.get("type") if item.get("type") in _QUIZ_TYPES else "short_answer",
                "question": str(item["question"]),
                "options": [str(o) for o in options] if isinstance(options, list) else [],
                "answer": str(item.get("answer") or ""),
                "explanation": str(item.get("explanation") or ""),
                "chunk_id": chunk_id if chunk_id in valid_chunk_ids else None,
            }
        )
    return questions or None


@router.post("/quiz")
async def generate_quiz(
    body: QuizRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """生成考点习题：单章代表 chunks → QUIZ_PROMPT → MEDIUM 档；
    LLM 输出非合法 JSON 时带提示重试一次，仍非法 502。"""
    started = time.monotonic()
    client, provider = await resolve_client(db, user, ModelTier.MEDIUM)
    if provider == "official":
        await check_quota(db, user)
    course = await _load_course(db, user, body.course_id)
    chapter = await db.get(Chapter, body.chapter_id)
    if chapter is None or chapter.course_id != course.id:
        raise HTTPException(status_code=404, detail="Not Found")

    try:
        chunks = await _chapter_reps(db, user, course, chapter, _QUIZ_CHUNK_LIMIT)
    except HTTPException:
        raise
    except Exception:
        logger.exception("习题生成检索失败 course_id=%s chapter_id=%s", course.id, chapter.id)
        raise HTTPException(status_code=502, detail="检索服务暂时不可用，请稍后重试") from None
    if not chunks:
        raise HTTPException(status_code=422, detail="该章节暂无可用于出题的资料")

    prompt = QUIZ_PROMPT.format(
        count=body.count,
        chapter_text=_sections_text([{"title": chapter.title, "chunks": chunks}]),
    )
    valid_chunk_ids = {h["chunk"].chunk_id for h in chunks}
    try:
        messages = [{"role": "user", "content": prompt}]
        raw, usage = await client.chat(messages)
        questions = _parse_quiz(raw, valid_chunk_ids)
        if questions is None:  # 非法 JSON：带提示重试一次
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": _QUIZ_RETRY_HINT},
            ]
            raw, usage = await client.chat(messages)
            questions = _parse_quiz(raw, valid_chunk_ids)
    except Exception:
        logger.exception("习题 LLM 生成失败 course_id=%s chapter_id=%s", course.id, chapter.id)
        raise HTTPException(status_code=502, detail="AI 服务暂时不可用，请稍后重试") from None
    if questions is None:
        raise HTTPException(status_code=502, detail="AI 习题生成格式异常，请重试")

    token_usage = (usage or {}).get("total_tokens", 0)
    latency_ms = int((time.monotonic() - started) * 1000)
    await _log_and_consume(
        db, user, course,
        question=f"[review] 习题生成：{course.name} / {chapter.title}（{body.count} 题）",
        prompt=prompt, answer=json.dumps(questions, ensure_ascii=False), provider=provider,
        token_usage=token_usage, latency_ms=latency_ms,
        chapter_ids=[chapter.id], top_chunks=[h["chunk"].chunk_id for h in chunks],
    )
    return {
        "course_id": course.id,
        "chapter": {"id": chapter.id, "title": chapter.title},
        "questions": questions,
        "provider": provider,
        "token_usage": token_usage,
        "latency_ms": latency_ms,
    }
