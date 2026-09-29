"""自动预检（模块 B2 第一级）：格式可读性 / MD5 查重 / 敏感词 / AI 初评（0-100 分）。

硬失败（格式损坏、重复、命中敏感词）→ 直接驳回并通知，附理由。
AI 初评仅供人工审核参考：LLM 异常或输出解析失败记入 error，不阻塞流程。
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.prompts import PRECHECK_PROMPT
from app.core.llm.router import ModelTier, get_official_client
from app.moderation.sensitive_words import find_sensitive_hits
from app.storage.models import Chunk, Course, Document, Resource

logger = logging.getLogger(__name__)

_SENSITIVE_SCAN_LIMIT = 20000  # 敏感词扫描文本长度上限
_AI_HEAD_LIMIT = 3000  # AI 初评取正文开头长度


def decide_hard_fail(
    format_ok: bool, duplicate: bool, sensitive_hits: list[str]
) -> tuple[bool, str]:
    """硬失败判定（纯函数）：任一不通过即驳回，多项理由用「；」连接。"""
    reasons = []
    if not format_ok:
        reasons.append("文档未成功解析或无有效内容")
    if duplicate:
        reasons.append("与审核中/已上架的资料内容重复")
    if sensitive_hits:
        reasons.append(f"命中敏感词：{'、'.join(sensitive_hits)}")
    return (bool(reasons), "；".join(reasons))


def _parse_ai_review(text: str) -> dict:
    """从 LLM 输出提取 JSON（容忍 ```json 代码围栏）：取第一个 { 到最后一个 } 子串。"""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("AI 初评输出中不含 JSON")
    return json.loads(text[start : end + 1])


async def _ai_review(course: Course, resource: Resource, chunks: list[Chunk]) -> dict:
    """AI 初评（SHORT 档）：失败返回 {"error": ...}，不阻塞审核流程。"""
    head = "\n".join(c.content for c in chunks)[:_AI_HEAD_LIMIT]
    document_text_head = f"课程：《{course.name}》\n资料标题：{resource.title}\n\n{head}"
    prompt = PRECHECK_PROMPT.format(document_text_head=document_text_head)
    try:
        text, _ = await get_official_client(ModelTier.SHORT).chat(
            [{"role": "user", "content": prompt}]
        )
        return _parse_ai_review(text)
    except Exception as exc:
        logger.warning("AI 初评失败（不阻塞）resource_id=%s: %s", resource.id, exc)
        return {"error": str(exc)}


async def run_precheck(session: AsyncSession, resource: Resource) -> dict:
    """对投稿资源执行自动预检，返回结果 dict（写 review_tasks.precheck_result）。"""
    doc = await session.get(Document, resource.document_id)
    chunks = (
        await session.execute(
            select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.id)
        )
    ).scalars().all()

    format_ok = doc.status == "parsed" and len(chunks) >= 1

    duplicate = (
        await session.execute(
            select(Resource.id)
            .join(Document, Document.id == Resource.document_id)
            .where(
                Resource.id != resource.id,
                Resource.review_status.in_(["pending", "co_reviewing", "approved"]),
                Document.md5 == doc.md5,
            )
            .limit(1)
        )
    ).first() is not None

    scan_text = (
        resource.title + "\n" + (resource.description or "") + "\n"
        + "\n".join(c.content for c in chunks)
    )[:_SENSITIVE_SCAN_LIMIT]
    sensitive_hits = find_sensitive_hits(scan_text)

    course = await session.get(Course, resource.course_id)
    ai_review = await _ai_review(course, resource, chunks)

    hard_fail, reason = decide_hard_fail(format_ok, duplicate, sensitive_hits)
    return {
        "format_ok": format_ok,
        "duplicate": duplicate,
        "sensitive_hits": sensitive_hits,
        "ai_review": ai_review,
        "hard_fail": hard_fail,
        "reason": reason,
    }
