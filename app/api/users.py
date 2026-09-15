"""用户接口（§12.1）：当前用户、AI 额度、自定义 Key、公开主页。"""

from fastapi import APIRouter

router = APIRouter(prefix="/users", tags=["users"])

# TODO(v0.2): GET/PATCH /me、GET /me/quota、PUT/DELETE /me/llm-key、GET /{id}/profile
