"""RAG 问答主流水线（模块 A3）：双库 scope 路由 → 粗召回 → 精排 → 生成 → 溯源。

检索范围路由（§5.3）：
- personal：仅个人库（owner_id 过滤，检索层与存储层双层执行）
- public：仅公共库（scope='public' 过滤，课程须 active）
- mixed：同一课程 ID 的双库串行检索，个人库结果加权 1.2 后 RRF 融合

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

from app.config import get_settings
from app.core.context import INDEX_VERSION, prompt_text
from app.core.embeddings import get_embedding
from app.core.llm.client import LLMClient
from app.core.llm.prompts import QA_SYSTEM_PROMPT
from app.core.llm.router import ModelTier, get_official_client
from app.core.retrieval.coarse import coarse_recall
from app.core.retrieval.fine import SearchResults, fine_search
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


async def _attach_chapter_titles(session, hits, titles: dict, course_id: int) -> dict:
    """全局补充/无摘要路径也从 PG 补齐标题，避免 Prompt 结构信息随回退丢失。"""
    missing = {
        chapter_id for hit in hits
        if (chapter_id := getattr(hit["chunk"], "chapter_id", None)) is not None
        and chapter_id not in titles
    }
    if missing:
        rows = (
            await session.execute(
                select(Chapter.id, Chapter.title).where(
                    Chapter.course_id == course_id, Chapter.id.in_(missing)
                )
            )
        ).all()
        titles = {**titles, **{chapter_id: title for chapter_id, title in rows}}
    for hit in hits:
        chapter_title = titles.get(getattr(hit["chunk"], "chapter_id", None))
        if chapter_title:
            hit["chunk"]._rag_chapter_title = chapter_title
    return titles


async def _recall_scope(
    session: AsyncSession,
    query: str,
    query_embedding: list[float],
    course_id: int,
    scope: str,
    owner_id: int | None,
    metrics: dict | None = None,
) -> tuple[list[dict], list[int] | None]:
    metrics = metrics if metrics is not None else {}
    coarse_started = time.monotonic()
    try:
        chapter_ids = await coarse_recall(session, query_embedding, course_id, scope)
        if chapter_ids is None:
            metrics.setdefault("fallback", []).append("no_coarse_summary")
    except Exception:
        chapter_ids = None
        metrics.setdefault("fallback", []).append("coarse_error")
        metrics.setdefault("degraded", []).append("coarse")
        logger.warning("章节粗召回失败，降级全课程 course=%s scope=%s", course_id, scope, exc_info=True)
    metrics["coarse_ms"] = round((time.monotonic() - coarse_started) * 1000, 3)
    metrics["coarse_candidates"] = len(chapter_ids or [])

    titles = getattr(chapter_ids, "chapter_titles", {})
    fine_started = time.monotonic()
    hits = await fine_search(
        session, query, query_embedding, scope, course_id,
        owner_id=owner_id, chapter_ids=chapter_ids,
    )
    metrics["fine_ms"] = round((time.monotonic() - fine_started) * 1000, 3)
    fine_metrics = getattr(hits, "metrics", {})
    for key in ("fallback", "degraded"):
        metrics.setdefault(key, []).extend(fine_metrics.get(key, []))
    for key, value in fine_metrics.items():
        if key not in {"fallback", "degraded"}:
            metrics[key] = value
    await _attach_chapter_titles(session, hits, titles, course_id)
    return hits, chapter_ids


async def retrieve(
    session: AsyncSession, query: str, user: User, course_id: int, scope: str
) -> tuple[list[dict], list[int] | None]:
    """返回命中与粗召回章节；命中列表携带完整、可序列化检索诊断。"""
    await check_course_access(session, user, course_id, scope)
    started = time.monotonic()
    metrics: dict = {
        "scope": scope,
        "fallback": [],
        "degraded": [],
        "target_index_version": INDEX_VERSION,
        "configured_embedding_model": get_settings().embedding_model,
    }
    embedding_started = time.monotonic()
    [query_embedding] = await get_embedding().embed([query])
    metrics["embedding_ms"] = round((time.monotonic() - embedding_started) * 1000, 3)

    if scope in ("personal", "public"):
        hits, chapter_ids = await _recall_scope(
            session, query, query_embedding, course_id, scope,
            owner_id=user.id if scope == "personal" else None, metrics=metrics,
        )
        result = SearchResults(hits, metrics=metrics)
    else:
        scope_metrics: dict[str, dict] = {"personal": {}, "public": {}}
        personal_hits, personal_chapters = await _recall_scope(
            session, query, query_embedding, course_id, "personal", owner_id=user.id,
            metrics=scope_metrics["personal"],
        )
        public_hits, public_chapters = await _recall_scope(
            session, query, query_embedding, course_id, "public", owner_id=None,
            metrics=scope_metrics["public"],
        )
        fused = rrf_fuse(
            [hit["chunk"] for hit in personal_hits],
            [hit["chunk"] for hit in public_hits],
            weight_a=get_settings().rag_mixed_personal_weight,
            weight_b=1.0,
            top_k=get_settings().rag_top_k,
        )
        combined = [
            *[("personal", hit) for hit in personal_hits],
            *[("public", hit) for hit in public_hits],
        ]
        by_id = {hit["chunk"].chunk_id: hit["chunk"] for _, hit in combined}
        hits = [
            {
                "chunk": by_id[chunk_id], "score": score,
                "route": next(route for route, hit in combined if hit["chunk"].chunk_id == chunk_id),
                "sources": next(
                    (hit.get("sources", []) for _, hit in combined if hit["chunk"].chunk_id == chunk_id),
                    [],
                ),
            }
            for chunk_id, score in fused if chunk_id in by_id
        ]
        chapter_ids = list(dict.fromkeys((personal_chapters or []) + (public_chapters or []))) or None
        metrics["scopes"] = scope_metrics
        for scoped in scope_metrics.values():
            metrics["fallback"].extend(scoped.get("fallback", []))
            metrics["degraded"].extend(scoped.get("degraded", []))
        versions = sorted({
            m["index_version"] for m in scope_metrics.values() if m.get("index_version")
        })
        models = sorted({
            m["embedding_model"] for m in scope_metrics.values() if m.get("embedding_model")
        })
        metrics["index_versions"] = versions
        metrics["embedding_models"] = models
        if len(versions) == 1:
            metrics["index_version"] = versions[0]
        if len(models) == 1:
            metrics["embedding_model"] = models[0]
        result = SearchResults(hits, metrics=metrics)
    metrics.setdefault("index_version", None)
    metrics.setdefault("embedding_model", None)
    metrics["fallback"] = list(dict.fromkeys(metrics["fallback"]))
    metrics["degraded"] = list(dict.fromkeys(metrics["degraded"]))
    metrics["returned_candidates"] = len(result)
    metrics["retrieval_ms"] = round((time.monotonic() - started) * 1000, 3)
    return result, chapter_ids


async def _build_citation_items(
    session: AsyncSession, cited_ids: list[str], hits: list[dict]
) -> list[dict]:
    """把引用编号映射到课程/章节/页码/原作者署名（§A3 溯源，公共库来源展示署名）。"""
    evidence_ids = {hit["chunk"].chunk_id for hit in hits}
    cited_ids = [chunk_id for chunk_id in cited_ids if chunk_id in evidence_ids]
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

    retrieval_metrics = dict(getattr(hits, "metrics", {}))
    if retrieval_metrics.get("weak_evidence"):
        hits = SearchResults([], metrics=retrieval_metrics)
    context = "\n\n".join(
        prompt_text(hit["chunk"], getattr(hit["chunk"], "_rag_chapter_title", None))
        for hit in hits
    )
    prompt = QA_SYSTEM_PROMPT.format(
        context=context or "（未检索到相关资料）", question=body.question
    )
    retrieval_metrics["prompt_chars"] = len(prompt)
    answer_parts: list[str] = []
    first_token_ms: int | None = None
    if hits:
        if client is None:
            client = get_official_client(ModelTier.SHORT)
        try:
            async for token in client.chat_stream([{"role": "system", "content": prompt}]):
                if first_token_ms is None:
                    first_token_ms = int((time.monotonic() - started) * 1000)
                answer_parts.append(token)
                yield {"type": "token", "text": token}
        except Exception:
            logger.exception("LLM 流式生成失败")
            yield {"type": "error", "message": "AI 服务暂时不可用，请稍后重试"}
            return
        answer = "".join(answer_parts)
        token_usage = (client.last_usage or {}).get("total_tokens", 0)
    else:
        answer = "资料中未提及该问题，当前课程资料不足，无法给出有依据的回答。"
        first_token_ms = int((time.monotonic() - started) * 1000)
        token_usage = 0
        yield {"type": "token", "text": answer}

    cited_ids = extract_citations(answer, {hit["chunk"].chunk_id for hit in hits})
    try:
        items = await _build_citation_items(session, cited_ids, hits)
    except Exception:
        logger.exception("引用映射查询失败（降级为空引用）")
        items = []
    yield {"type": "citations", "items": items}

    latency_ms = int((time.monotonic() - started) * 1000)
    retrieval_metrics["first_token_ms"] = first_token_ms
    retrieval_metrics["total_ms"] = latency_ms
    try:
        session.add(
            QaLog(
                user_id=user.id,
                question=body.question,
                course_id=body.course_id,
                search_scope=body.scope,
                coarse_chapters={"chapter_ids": coarse_ids} if coarse_ids else None,
                top_chunks=[hit["chunk"].chunk_id for hit in hits],
                prompt=prompt,
                answer=answer,
                model_provider=provider,
                token_usage=token_usage,
                latency_ms=latency_ms,
                retrieval_metrics=retrieval_metrics,
            )
        )
        if hits and provider != "user_custom":
            await consume_quota(session, user)
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("qa_logs 落库 / 额度扣减失败")
    yield {"type": "done", "token_usage": token_usage, "latency_ms": latency_ms}
