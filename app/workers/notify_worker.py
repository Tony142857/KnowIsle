"""站内通知任务（模块 B5）。

事件源：审核结果、回答被采纳、悬赏被响应、信用处罚、关注课程有新资料。
写入 notifications 表，页面导航栏未读角标（HTMX 每 30s 轮询）。
"""

from app.storage.db import SessionLocal
from app.storage.models import Notification


async def send_notification(ctx: dict, user_id: int, type_: str, title: str,
                            body: str = "", link: str = "") -> None:
    """ARQ 任务：写一条站内通知并提交。"""
    async with SessionLocal() as session:
        session.add(
            Notification(
                user_id=user_id, type=type_, title=title, body=body or None, link=link or None
            )
        )
        await session.commit()
