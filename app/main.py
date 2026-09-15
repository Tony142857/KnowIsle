"""知屿 KnowIsle 应用入口：中间件、路由注册（文档 §14）。"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import api_router
from app.config import get_settings
from app.web.routes import pages

BASE_DIR = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=f"{settings.site_name} KnowIsle")

    app.mount("/static", StaticFiles(directory=BASE_DIR / "web" / "static"), name="static")
    app.include_router(api_router, prefix="/api")
    app.include_router(pages.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
