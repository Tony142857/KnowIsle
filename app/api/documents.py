"""资料接口（§12.1）：上传个人库（异步解析）、解析状态轮询、投稿公共库。"""

from fastapi import APIRouter

router = APIRouter(prefix="/documents", tags=["documents"])

# TODO(v0.3): POST /（202 异步解析）、GET /{id}/status、POST /{id}/submit
