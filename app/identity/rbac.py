"""RBAC 角色权限（§3.2）：student / reviewer / builder / admin。

- 路由级：Depends(require_role("reviewer")) 角色门控
- 数据级：个人库强制 owner_id 过滤，公共库强制 scope='public'，越权返回 404（而非 403）
- 鉴权失败（未登录/令牌无效）返回 401；权限不足返回 404（§12 统一约定）

reviewer 与 builder 为平行特权角色（互不蕴含）；admin 为最高角色，放行所有角色门控。
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.identity.session import resolve_session, verify_jwt
from app.storage.cache import get_redis
from app.storage.db import get_db
from app.storage.models import User

ROLES = ("student", "reviewer", "builder", "admin")


async def _resolve_user_id(request: Request) -> int | None:
    """双通道解析：优先 Bearer JWT（API），其次会话 Cookie（页面）。"""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return verify_jwt(auth.removeprefix("Bearer ").strip())
    token = request.cookies.get(get_settings().session_cookie_name, "")
    if token:
        return await resolve_session(get_redis(), token)
    return None


async def get_current_user_optional(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> User | None:
    """页面渲染用：未登录返回 None 而不是 401。"""
    user_id = await _resolve_user_id(request)
    if user_id is None:
        return None
    user = await db.get(User, user_id)
    if user is None or user.status == "frozen":
        return None
    return user


async def get_current_user(
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> User:
    """API 鉴权依赖：未登录/账号冻结返回 401。"""
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


def require_role(*roles: str):
    """路由级角色门控依赖：角色不足返回 404（避免暴露资源存在性，§3.2）。

    reviewer 与 builder 为平行特权角色，互不蕴含；admin 为最高角色，放行所有门控。
    """
    invalid = set(roles) - set(ROLES)
    if invalid:
        raise ValueError(f"未知角色: {invalid}")
    allowed = set(roles) | {"admin"}

    async def dependency(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role not in allowed:
            raise HTTPException(status_code=404, detail="Not Found")
        return user

    return dependency


# 常用组合
require_student = get_current_user  # 登录即学生（§3.2：认证登录即得）
require_reviewer = require_role("reviewer", "admin")
require_builder = require_role("builder", "admin")
require_admin = require_role("admin")
