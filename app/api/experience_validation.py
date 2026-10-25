"""经验帖服务端结构校验，作为帖子路由依赖复用既有发帖端点。"""

from fastapi import HTTPException, Request

from app.community.experience import validate_experience_content


async def validate_experience_request(request: Request) -> None:
    try:
        payload = await request.json()
    except Exception:
        return
    if not isinstance(payload, dict) or payload.get("board") != "experience":
        return
    try:
        validate_experience_content(str(payload.get("content") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
