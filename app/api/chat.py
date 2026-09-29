"""RAG 问答接口（§12.1）：SSE 流式问答、检索链路详情。"""

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.core import pipeline
from app.identity.quota import check_quota
from app.identity.rbac import get_current_user
from app.storage.db import get_db
from app.storage.models import User

router = APIRouter(tags=["rag"])


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    course_id: int
    scope: Literal["personal", "public", "mixed"] = "personal"


@router.post("/chat")
async def chat(
    body: ChatRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """流式问答（SSE）：额度超限在进入事件流前直接返回 429 JSON，越权课程 404。"""
    await check_quota(db, user)
    await pipeline.check_course_access(db, user, body.course_id, body.scope)

    async def events():
        async for event in pipeline.answer_question(db, user, body):
            yield {"data": json.dumps(event, ensure_ascii=False)}

    return EventSourceResponse(events())


@router.post("/retrieval/trace")
async def retrieval_trace(
    body: ChatRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """检索链路详情（调试）：返回命中 chunk 列表与融合得分，不消耗额度、不调用 LLM。"""
    hits, _ = await pipeline.retrieve(db, body.question, user, body.course_id, body.scope)
    return {
        "question": body.question,
        "course_id": body.course_id,
        "scope": body.scope,
        "hits": [
            {
                "chunk_id": h["chunk"].chunk_id,
                "section": h["chunk"].section,
                "chapter_id": h["chunk"].chapter_id,
                "page_num": h["chunk"].page_num,
                "score": round(h["score"], 6),
                "snippet": h["chunk"].content[:80],
            }
            for h in hits
        ],
    }
