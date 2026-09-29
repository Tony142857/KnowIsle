"""知屿 KnowIsle 应用入口：中间件、路由注册（文档 §14）。"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import api_router
from app.config import get_settings
from app.storage.object_store import ensure_bucket
from app.storage.vector_store import (
    COLLECTION_PERSONAL,
    COLLECTION_PUBLIC,
    get_vector_store,
)
from app.web.routes import pages

BASE_DIR = Path(__file__).resolve().parent

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动预热：确保 S3 存储桶与 Chroma 双 Collection 就位（失败记日志不阻断启动）。"""
    try:
        await ensure_bucket()
    except Exception:
        logger.warning("启动预热：S3 ensure_bucket 失败（对象存储暂不可用）", exc_info=True)
    try:
        store = get_vector_store()
        await store.get_or_create_collection(COLLECTION_PUBLIC)
        await store.get_or_create_collection(COLLECTION_PERSONAL)
    except Exception:
        logger.warning("启动预热：Chroma collection 预建失败（向量库暂不可用）", exc_info=True)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=f"{settings.site_name} KnowIsle", lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=BASE_DIR / "web" / "static"), name="static")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    app.include_router(api_router, prefix="/api")
    app.include_router(pages.router)  # 含 /{page} 占位通配，必须最后注册

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        """API 请求返回统一错误体 {"detail": ...}（§12）；页面请求 404 渲染模板。

        越权访问统一返回 404 而非 403，避免暴露资源存在性（§3.2）。
        """
        if request.url.path.startswith("/api/") or exc.status_code != 404:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")
        return templates.TemplateResponse(
            request, "404.html", {"active": ""}, status_code=404
        )

    return app


app = create_app()
