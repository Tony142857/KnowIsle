"""站内通知任务（模块 B5）。

事件源：审核结果、回答被采纳、悬赏被响应、信用处罚、关注课程有新资料。
写入 notifications 表，页面导航栏未读角标（HTMX 每 30s 轮询）。
"""


async def send_notification(ctx: dict, user_id: int, type_: str, title: str,
                            body: str = "", link: str = "") -> None:
    # TODO(v0.6)
    raise NotImplementedError("通知任务尚未实现（v0.6 迭代交付）")
