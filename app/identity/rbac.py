"""RBAC 角色权限（§3.2）：student / reviewer / builder / admin。

- 路由级：Depends(require_role("reviewer")) 角色门控
- 数据级：个人库强制 owner_id 过滤，公共库强制 scope='public'，越权返回 404（而非 403）
"""

from fastapi import HTTPException

ROLES = ("student", "reviewer", "builder", "admin")


def require_role(*roles: str):
    """路由级角色门控依赖（占位实现，认证体系接入后启用）。"""
    invalid = set(roles) - set(ROLES)
    if invalid:
        raise ValueError(f"未知角色: {invalid}")

    async def dependency():
        # TODO(v0.2): 解析会话/JWT 当前用户 → 校验角色 → 不足返回 404
        raise HTTPException(status_code=501, detail="认证体系尚未接入（v0.2 迭代交付）")

    return dependency
