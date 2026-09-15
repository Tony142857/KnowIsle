"""公共资源接口（§12.1）：检索、详情与预览、下载（积分校验）、克隆到个人库。"""

from fastapi import APIRouter

router = APIRouter(prefix="/resources", tags=["resources"])

# TODO(v0.4): GET /、GET /{id}、GET /{id}/download、POST /{id}/clone
