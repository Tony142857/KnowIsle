"""举报接口（§12.1）：资源 / 帖子 / 评论 / 用户举报。"""

from fastapi import APIRouter

router = APIRouter(prefix="/reports", tags=["reports"])

# TODO(v0.8): POST /
