"""REST API 路由聚合（接口总表见文档 §12.1）。页面渲染与 API 同源提供。"""

from fastapi import APIRouter

from app.api import (
    admin,
    auth,
    chat,
    courses,
    documents,
    notifications,
    posts,
    reports,
    resources,
    review,
    review_tasks,
    users,
    votes,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(courses.router)
api_router.include_router(documents.router)
api_router.include_router(resources.router)
api_router.include_router(chat.router)
api_router.include_router(review.router)
api_router.include_router(posts.router)
api_router.include_router(votes.router)
api_router.include_router(reports.router)
api_router.include_router(notifications.router)
api_router.include_router(review_tasks.router)
api_router.include_router(admin.router)
