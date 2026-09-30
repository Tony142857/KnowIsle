"""投票服务（模块 B3）：点赞/有用，(user_id, target_type, target_id) 唯一约束防刷。"""

VOTE_TARGET_MODELS = ("post", "comment", "resource")


def decide_vote(existing_value: int | None, value: int) -> tuple[str, int]:
    """投票开关状态机（纯函数）：返回 (操作, 操作后的 my_vote)。

    无记录 → insert；已有同值 → delete（取消）；已有异值 → update（改值）。
    """
    if existing_value is None:
        return ("insert", value)
    if existing_value == value:
        return ("delete", 0)
    return ("update", value)
