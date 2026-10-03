"""RAG 问答主流水线（模块 A3）：双库 scope 路由 → 粗召回 → 精排 → 生成 → 溯源。

检索范围路由（§5.3）：
- personal：仅个人库（owner_id 过滤，检索层与存储层双层执行）
- public：仅公共库（scope='public' 过滤，课程须 active）
- mixed：双库并行检索，个人库结果加权 1.2 后 RRF 融合

answer_question 产出 SSE 事件流：token → citations → done（异常时 error）。
额度检查（429）与课程权限检查（404）由路由层在进入生成器前完成。
"""

import logging
import re
import time
from collections.abc import AsyncIterator

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embeddings import get_embedding
from app.core.llm.client import LLMClient
from app.core.llm.prompts import QA_SYSTEM_PROMPT
from app.core.llm.router import ModelTier, get_official_client
from app.core.retrieval.coarse import coarse_recall
from app.core.retrieval.fine import fine_search
from app.core.retrieval.fusion import rrf_fuse
from app.identity.quota import consume_quota
from app.storage.models import Chapter, Chunk, Course, Document, QaLog, User

logger = logging.getLogger(__name__)

_CITATION_RE = re.compile(r"\[((?:per|pub)_d\d+_\d{4,})\]")


def extract_citations(answer: str, valid_chunk_ids: set[str]) -> list[str]:
    """提取回答中出现的 [chunk_id] 引用，仅保留上下文真实存在的，去重并保持出现顺序。"""
    seen: list[str] = []
    for match in _CITATION_RE.finditer(answer):
        chunk_id = match.group(1)
        if chunk_id in valid_chunk_ids and chunk_id not in seen:
            seen.append(chunk_id)
    return seen


async def check_course_access(
    session: AsyncSession, user: User, course_id: int, scope: str
) -> Course:
    """课程存在性与数据级权限检查（越权 404，§3.2）。"""
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="Not Found")
    if scope == "personal":
        if course.scope != "personal" or course.owner_id != user.id:
            raise HTTPException(status_code=404, detail="Not Found")
    elif scope == "public":
        if course.scope != "public" or course.status != "active":
            raise HTTPException(status_code=404, detail="Not Found")
    else:  # mixed：课程本身可能是个人课或公共课，均需通过归属/状态校验
        if course.scope == "personal" and course.owner_id != user.id:
            raise HTTPException(status_code=404, detail="Not Found")
        if course.scope == "public" and course.status != "active":
            raise HTTPException(status_code=404, detail="Not Found")
    return course


async def retrieve(
    session: AsyncSession, query: str, user: User, course_id: int, scope: str
) -> tuple[list[dict], list[int] | None]:
    """检索入口（§5.3）：返回 (按得分降序的命中列表, 粗召回章节 ID 或 None)。

    coarse 返回 None（无章节摘要）时降级为全课程范围精排（§8.3）。
    """
    await check_course_access(session, user, course_id, scope)
    [query_embedding] = await get_embedding().embed([query])

    if scope in ("personal", "public"):
        chapter_ids = await coarse_recall(session, query_embedding, course_id, scope)
        hits = await fine_search(
            session, query, query_embedding, scope, course_id,
            owner_id=user.id, chapter_ids=chapter_ids,
        )
        return hits, chapter_ids

    # mixed：双库检索，个人库结果加权 1.2 后 RRF 融合（§5.3）。
    # 注：同一 AsyncSession 不支持并发操作，两路串行执行（单连接会话限制）。
    personal_hits = await fine_search(
        session, query, query_embedding, "personal", course_id, owner_id=user.id
    )
    public_hits = await fine_search(
        session, query, query_embedding, "public", course_id, owner_id=None
    )
    fused = rrf_fuse(
        [h["chunk"] for h in personal_hits],
        [h["chunk"] for h in public_hits],
        weight_semantic=1.2,
        weight_bm25=1.0,
        top_k=5,
    )
    by_id = {h["chunk"].chunk_id: h["chunk"] for h in personal_hits + public_hits}
    hits = [
        {"chunk": by_id[chunk_id], "score": score}
        for chunk_id, score in fused
        if chunk_id in by_id
    ]
    return hits, None


async def _build_citation_items(
    session: AsyncSession, cited_ids: list[str], hits: list[dict]
) -> list[dict]:
    """把引用编号映射到课程/章节/页码/原作者署名（§A3 溯源，公共库来源展示署名）。"""
    if not cited_ids:
        return []
    rows = (
        await session.execute(
            select(Chunk, Document, User.nickname, Chapter.title)
            .join(Document, Document.id == Chunk.document_id)
            .join(User, User.id == Chunk.owner_id)
            .outerjoin(Chapter, Chapter.id == Chunk.chapter_id)
            .where(Chunk.chunk_id.in_(cited_ids))
        )
    ).all()
    info = {
        chunk.chunk_id: (chunk, doc, nickname, chapter_title)
        for chunk, doc, nickname, chapter_title in rows
    }
    items = []
    for chunk_id in cited_ids:
        entry = info.get(chunk_id)
        if entry is None:
            continue
        chunk, doc, nickname, chapter_title = entry
        items.append(
            {
                "chunk_id": chunk.chunk_id,
                "document_id": doc.id,
                "file_name": doc.file_name,
                "chapter": chapter_title,
                "section": chunk.section,
                "page_num": chunk.page_num,
                "source_scope": chunk.scope,
                "uploader": nickname,
                "snippet": chunk.content[:80],
            }
        )
    return items


async def answer_question(
    session: AsyncSession,
    user: User,
    body,
    client: LLMClient | None = None,
    provider: str = "official",
) -> AsyncIterator[dict]:
    """问答主流程：检索 → 组装 Prompt → 流式生成 → 引用映射 → qa_logs 落库 → 扣减额度。

    body 需有 question / course_id / scope 属性。LLM/检索异常时 yield error 事件而不是抛出。
    client 为 None 时使用官方 SHORT 档模型；provider="user_custom" 时不占官方额度。
    """
    started = time.monotonic()
    try:
        hits, coarse_ids = await retrieve(session, body.question, user, body.course_id, body.scope)
    except HTTPException as exc:
        yield {"type": "error", "message": exc.detail}
        return
    except Exception:
        logger.exception("RAG 检索失败")
        yield {"type": "error", "message": "检索服务暂时不可用，请稍后重试"}
        return

    context = "\n\n".join(
        f"[{h['chunk'].chunk_id}]\n{h['chunk'].content}" for h in hits
    )
    prompt = QA_SYSTEM_PROMPT.format(context=context or "（未检索到相关资料）", question=body.question)
    if client is None:
        client = get_official_client(ModelTier.SHORT)

    answer_parts: list[str] = []
    try:
        async for token in client.chat_stream([{"role": "system", "content": prompt}]):
            answer_parts.append(token)
            yield {"type": "token", "text": token}
    except Exception:
        logger.exception("LLM 流式生成失败")
        yield {"type": "error", "message": "AI 服务暂时不可用，请稍后重试"}
        return

    answer = "".join(answer_parts)
    cited_ids = extract_citations(answer, {h["chunk"].chunk_id for h in hits})
    try:
        items = await _build_citation_items(session, cited_ids, hits)
    except Exception:
        # token 已流出：引用映射查询异常降级为空引用继续走 done，
        # 避免 SSE 流中断（客户端拿不到 done/error、qa_logs 不落库）
        logger.exception("引用映射查询失败（降级为空引用）")
        items = []
    yield {"type": "citations", "items": items}

    token_usage = (client.last_usage or {}).get("total_tokens", 0)
    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        session.add(
            QaLog(
                user_id=user.id,
                question=body.question,
                course_id=body.course_id,
                search_scope=body.scope,
                coarse_chapters={"chapter_ids": coarse_ids} if coarse_ids else None,
                top_chunks=[h["chunk"].chunk_id for h in hits],
                prompt=prompt,
                answer=answer,
                model_provider=provider,
                token_usage=token_usage,
                latency_ms=latency_ms,
            )
        )
        if provider != "user_custom":  # 自定义 Key 不占官方额度
            await consume_quota(session, user)
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("qa_logs 落库 / 额度扣减失败")
    yield {"type": "done", "token_usage": token_usage, "latency_ms": latency_ms}
