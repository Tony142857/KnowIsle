"""协审工作台接口（§12.1）：待审任务队列、提交协审意见（协审员角色）。"""

from fastapi import APIRouter

router = APIRouter(prefix="/review", tags=["review-tasks"])

# TODO(v0.4): GET /tasks、POST /tasks/{id}/verdict
