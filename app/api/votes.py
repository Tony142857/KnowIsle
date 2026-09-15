"""投票接口（§12.1）：点赞 / 有用。"""

from fastapi import APIRouter

router = APIRouter(prefix="/votes", tags=["votes"])

# TODO(v0.5): POST /
