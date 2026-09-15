"""站内通知接口（§12.1）：通知列表、标记已读。"""

from fastapi import APIRouter

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/unread-count")
async def unread_count() -> dict:
    """导航栏未读角标轮询（HTMX 每 30s）。

    TODO(v0.6): 接入登录态后按当前用户统计 notifications 未读数。
    """
    return {"unread": 0}

# TODO(v0.6): GET /、POST /read
