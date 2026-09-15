"""页面路由（§13）：服务端渲染入口，按板块拆分到本包内各模块。"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")


@router.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@router.get("/login")
async def login(request: Request):
    return templates.TemplateResponse(request, "login.html")


# TODO(v0.2+): /majors、/courses/{id}、/library、/boards/{board}、/posts/{id}、
#              /me、/review、/admin 等页面路由按板块拆分为独立模块
