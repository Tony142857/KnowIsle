"""社区帖子接口（§12.1）：帖子列表/发帖、详情（含 AI 首答）、评论、采纳。"""

from fastapi import APIRouter

router = APIRouter(prefix="/posts", tags=["posts"])

# TODO(v0.5): GET/POST /、GET /{id}、GET/POST /{id}/comments、POST /comments/{id}/accept
