"""认证接口（§12.1）：CAS/OIDC 登录跳转与回调、校园邮箱降级验证。"""

from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])

# TODO(v0.2): GET /cas/login、GET /cas/callback、POST /email/verify
