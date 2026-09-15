"""站内通知接口（§12.1）：通知列表、标记已读。"""

from fastapi import APIRouter

router = APIRouter(prefix="/notifications", tags=["notifications"])

# TODO(v0.6): GET /、POST /read
