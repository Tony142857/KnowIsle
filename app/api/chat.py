"""RAG 问答接口（§12.1）：SSE 流式问答、检索链路详情。"""

from fastapi import APIRouter

router = APIRouter(tags=["rag"])

# TODO(v0.3): POST /chat（SSE）、POST /retrieval/trace
