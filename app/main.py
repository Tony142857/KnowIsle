"""知屿 KnowIsle 应用入口：中间件、路由注册（文档 §14）。"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import api_router
from app.config import get_settings
from app.web.routes import pages

BASE_DIR = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=f"{settings.site_name} KnowIsle")

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
