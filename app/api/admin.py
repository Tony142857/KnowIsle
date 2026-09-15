"""管理后台接口（§12.1）：终审、空间管理、用户治理、运营看板。"""

from fastapi import APIRouter

router = APIRouter(prefix="/admin", tags=["admin"])

# TODO(v0.4/v0.6): POST /review/final、POST/PATCH /majors /courses、
#                  PUT /users/{id}/role、POST /users/{id}/credit、GET /dashboard
