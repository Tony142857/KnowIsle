"""页面路由（§13）：服务端渲染入口，按板块拆分到本包内各模块。"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")

# 社区板块（§4.2），用于 /boards/{board} 校验
BOARDS = {
    "qa": ("知屿问答", "v0.5"),
    "experience": ("经验长廊", "v0.7"),
    "bounty": ("资料求援", "v0.7"),
    "discuss": ("讨论区", "v0.5"),
}

# 板块占位页配置：路由 → (板块名, 交付版本, 说明, 导航高亮键)
PLACEHOLDER_PAGES = {
    "majors": ("课程空间", "v0.2", "专业 → 课程 → 资料库 + AI 问答 + 讨论 + 贡献榜", "majors"),
    "library": ("个人知识库", "v0.3", "私有资料上传 → 结构化 → 问答 / 溯源 / 大纲 / 习题 / 串讲", "library"),
    "me": ("个人中心", "v0.6", "成长看板 / 我的上传与审核进度 / 收藏关注 / 通知 / AI 额度与 Key 配置", ""),
}


@router.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"active": ""})


@router.get("/login")
async def login(request: Request):
    return templates.TemplateResponse(request, "login.html", {"active": ""})


@router.get("/boards/{board}")
async def board(request: Request, board: str):
    if board not in BOARDS:
        raise HTTPException(status_code=404)
    name, version = BOARDS[board]
    return templates.TemplateResponse(
        request, "placeholder.html",
        {"name": name, "version": version, "note": "", "active": board},
    )


@router.get("/{page}")
async def placeholder_page(request: Request, page: str):
    """板块占位页：/majors、/library、/me（其余路径交给 FastAPI 默认 404）。"""
    if page not in PLACEHOLDER_PAGES:
        raise HTTPException(status_code=404)
    name, version, note, active = PLACEHOLDER_PAGES[page]
    return templates.TemplateResponse(
        request, "placeholder.html",
        {"name": name, "version": version, "note": note, "active": active},
    )


# TODO(v0.2+): 各板块页面就绪后删除占位路由，按板块拆分为独立路由模块
