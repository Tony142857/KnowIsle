"""RAG 复习工具接口（§12.1）：大纲 / 习题 / 串讲生成（模块 A4）。"""

from fastapi import APIRouter

router = APIRouter(prefix="/review", tags=["review-tools"])

# TODO(v0.3): POST /outline、POST /quiz、POST /final（串讲为二期，保留路由占位）
