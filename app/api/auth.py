"""认证接口（§12.1）：CAS/OIDC 登录跳转与回调（预留）、校园邮箱降级验证、会话/JWT。

统一约定（§12）：鉴权失败 401；参数错误 422；频率超限 429。
邮箱降级为开发期默认通道（§3.1），验证码假通道见 identity/email_fallback.py。
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.identity import cas, email_fallback
from app.identity.rbac import get_current_user
from app.identity.session import create_session, destroy_session, issue_jwt
from app.storage.cache import get_redis
from app.storage.db import get_db
from app.storage.models import AuthIdentity, Major, User

logger = logging.getLogger("knowisle.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

PROVIDER = "email_fallback"


class EmailCodeRequest(BaseModel):
    student_no: str = Field(min_length=1, max_length=32)
    email: EmailStr


class EmailVerifyRequest(BaseModel):
    student_no: str = Field(min_length=1, max_length=32)
    email: EmailStr
    code: str = Field(min_length=6, max_length=6)
    # 首次登录建档补全（§3.1：邮箱通道拿不到学籍信息，需用户补全；老用户忽略）
    real_name: str | None = Field(default=None, max_length=64)
    nickname: str | None = Field(default=None, min_length=2, max_length=32)
    grade: str | None = Field(default=None, max_length=16)
    major_id: int | None = None


class UserBrief(BaseModel):
    id: int
    student_no: str
    nickname: str
    avatar_url: str | None
    role: str


def _user_brief(user: User) -> dict:
    return UserBrief(
        id=user.id,
        student_no=user.student_no,
        nickname=user.nickname,
        avatar_url=user.avatar_url,
        role=user.role,
    ).model_dump()


async def _issue_session(response: Response, user_id: int) -> None:
    settings = get_settings()
    token = await create_session(get_redis(), user_id)
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
    )


# ---------------------------------------------------------------------------
# CAS / OIDC（§3.1 最终对接目标；v0.2 仅预留抽象与路由，不对接）
# ---------------------------------------------------------------------------


@router.get("/cas/login")
async def cas_login():
    if cas.current_provider() == PROVIDER:
        raise HTTPException(
            status_code=501,
            detail="当前为邮箱降级通道（AUTH_PROVIDER=email_fallback），请使用 /login 页面登录",
        )
    raise HTTPException(status_code=501, detail="CAS/OIDC 对接未启用（v0.2 仅预留抽象）")


@router.get("/cas/callback")
async def cas_callback():
    raise HTTPException(status_code=501, detail="CAS/OIDC 对接未启用（v0.2 仅预留抽象）")


# ---------------------------------------------------------------------------
# 邮箱降级通道（§3.1 开发期默认）
# ---------------------------------------------------------------------------


@router.post("/email/code")
async def send_email_code(payload: EmailCodeRequest):
    """发送校园邮箱验证码（开发期假通道：写日志 + 响应回显 dev_code）。"""
    if not email_fallback.check_email_allowed(payload.email):
        raise HTTPException(status_code=422, detail="仅允许校园邮箱（edu 域名）")
    redis = get_redis()
    dev_code, cooldown = await email_fallback.send_code(
        redis, payload.student_no, payload.email
    )
    if cooldown > 0:
        raise HTTPException(status_code=429, detail=f"发送过于频繁，请 {cooldown} 秒后重试")
    resp: dict = {"sent": True, "expires_in": get_settings().email_code_ttl_seconds}
    if dev_code is not None:
        resp["dev_code"] = dev_code  # 开发期假通道回显（email_code_echo），生产关闭
    return resp


@router.post("/email/verify")
async def verify_email_code(
    payload: EmailVerifyRequest, response: Response, db: Annotated[AsyncSession, Depends(get_db)]
):
    """验证码校验：老用户直接登录；新用户补全资料后建档并登录（§3.1 按学号建档）。

    验证码先校验不销毁，登录/建档成功才消耗——避免「需补全资料」中间态吞掉验证码。
    """
    redis = get_redis()
    if not await email_fallback.verify_code(redis, payload.student_no, payload.code,
                                            consume=False):
        raise HTTPException(status_code=401, detail="验证码错误或已过期")

    user = (
        await db.execute(select(User).where(User.student_no == payload.student_no))
    ).scalar_one_or_none()

    if user is not None:
        identity = (
            await db.execute(
                select(AuthIdentity).where(
                    AuthIdentity.provider == PROVIDER,
                    AuthIdentity.external_id == payload.student_no,
                )
            )
        ).scalar_one_or_none()
        bound_email = (identity.raw_profile or {}).get("email") if identity else None
        if bound_email and bound_email.lower() != payload.email.lower():
            raise HTTPException(status_code=401, detail="学号与绑定邮箱不一致")
        await email_fallback.verify_code(redis, payload.student_no, payload.code)
        await _issue_session(response, user.id)
        return {"need_profile": False, "user": _user_brief(user)}

    # 首次登录：需补全学籍资料后建档（社区内仅展示昵称，§3.1 隐私要求）
    if not (payload.real_name and payload.nickname):
        return {"need_profile": True, "required": ["real_name", "nickname", "grade", "major_id"]}

    if payload.major_id is not None and await db.get(Major, payload.major_id) is None:
        raise HTTPException(status_code=422, detail="专业不存在")
    nickname_taken = (
        await db.execute(select(User.id).where(User.nickname == payload.nickname))
    ).scalar_one_or_none()
    if nickname_taken is not None:
        raise HTTPException(status_code=422, detail="昵称已被使用")

    user = User(
        student_no=payload.student_no,
        real_name=payload.real_name,
        nickname=payload.nickname,
        grade=payload.grade,
        major_id=payload.major_id,
    )
    db.add(user)
    await db.flush()
    db.add(
        AuthIdentity(
            user_id=user.id,
            provider=PROVIDER,
            external_id=payload.student_no,
            raw_profile={
                "email": payload.email,
                "grade": payload.grade,
                "registered_via": PROVIDER,
            },
        )
    )
    await db.commit()
    await email_fallback.verify_code(redis, payload.student_no, payload.code)
    logger.info("[email_fallback] 新用户建档 student_no=%s user_id=%s", payload.student_no, user.id)
    await _issue_session(response, user.id)
    return {"need_profile": False, "user": _user_brief(user)}


# ---------------------------------------------------------------------------
# 会话与令牌（§7.4 双通道）
# ---------------------------------------------------------------------------


@router.post("/logout")
async def logout(request: Request, response: Response):
    """销毁 Redis 会话并清除 Cookie。"""
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name, "")
    await destroy_session(get_redis(), token)
    response.delete_cookie(settings.session_cookie_name)
    return {"logged_out": True}


@router.post("/token")
async def issue_api_token(user: Annotated[User, Depends(get_current_user)]):
    """凭有效会话签发 API 通道 JWT（桌面客户端 / 脚本调用使用）。"""
    return {"access_token": issue_jwt(user.id), "token_type": "bearer",
            "expires_in": get_settings().jwt_ttl_seconds}
