"""空间接口（§12.1）：专业列表、课程空间、章节知识树。"""

from fastapi import APIRouter

router = APIRouter(tags=["courses"])

# TODO(v0.2): GET /majors、GET/POST /courses、GET /courses/{id}、GET /courses/{id}/chapters
