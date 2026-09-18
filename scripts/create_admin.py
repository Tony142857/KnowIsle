"""初始化管理员账号（§15.4）。

用法：python scripts/create_admin.py --student-no 20230001 --nickname 管理员 \
        --real-name 张三 --email admin@stu.example.edu.cn

幂等：学号已存在时提升为 admin 并确保 email_fallback 身份绑定存在。
管理员登录：走 /login 邮箱降级通道（学号 + 此处绑定的邮箱 + 验证码）。
"""

import argparse
import asyncio

from sqlalchemy import select

from app.storage.db import SessionLocal
from app.storage.models import AuthIdentity, User

PROVIDER = "email_fallback"


async def create_admin(student_no: str, nickname: str, real_name: str, email: str) -> str:
    async with SessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.student_no == student_no))
        ).scalar_one_or_none()
        if user is None:
            nickname_taken = (
                await db.execute(select(User.id).where(User.nickname == nickname))
            ).scalar_one_or_none()
            if nickname_taken is not None:
                return f"错误：昵称「{nickname}」已被使用"
            user = User(student_no=student_no, real_name=real_name,
                        nickname=nickname, role="admin")
            db.add(user)
            await db.flush()
            action = f"已创建管理员 user_id={user.id}"
        else:
            user.role = "admin"
            action = f"用户已存在，已提升为管理员 user_id={user.id}"

        identity = (
            await db.execute(
                select(AuthIdentity).where(AuthIdentity.provider == PROVIDER,
                                           AuthIdentity.external_id == student_no)
            )
        ).scalar_one_or_none()
        if identity is None:
            db.add(AuthIdentity(user_id=user.id, provider=PROVIDER,
                                external_id=student_no,
                                raw_profile={"email": email, "registered_via": "create_admin"}))
        else:
            identity.raw_profile = {**(identity.raw_profile or {}), "email": email}
        await db.commit()
    return f"{action}；登录邮箱：{email}"


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 / 提升管理员账号")
    parser.add_argument("--student-no", required=True, help="学号（认证锚点，一人一号）")
    parser.add_argument("--nickname", required=True, help="昵称（社区内展示，唯一）")
    parser.add_argument("--real-name", default="管理员", help="真实姓名（仅核验用，不公开）")
    parser.add_argument("--email", required=True,
                        help="绑定邮箱（管理员登录邮箱降级通道时使用）")
    args = parser.parse_args()
    print(asyncio.run(create_admin(args.student_no, args.nickname, args.real_name, args.email)))


if __name__ == "__main__":
    main()
