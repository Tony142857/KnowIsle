"""评论服务（模块 B3）：楼中楼评论、采纳最佳答案（自问自答不计分）。"""

SCORE_ANSWER_ACCEPTED = 15  # 回答被采纳


def validate_accept(
    board: str,
    post_author_id: int,
    operator_id: int,
    comment_author_id: int,
    already_accepted: bool,
) -> str | None:
    """采纳资格判定（纯函数）：返回错误码，None 表示可采纳。

    not_owner → 404（越权不暴露存在性）；not_board / self_response / already_settled → 422。
    v0.7 起问答贴与求援贴均可采纳（求援采纳 = 悬赏结算）。求援帖口径（v0.7 修订）：
    帖主不可采纳自己的响应（自响应不结算的入口拦截）；已结算后不可改采其他评论
    （改采不转移赏金，结算即终态）。qa 帖不变：自问自答可采纳但 0 分、改采只换标记。

    already_accepted 由调用方按「已采纳且非本评论」计算，重复采纳同一条评论的
    幂等分支不受影响。
    """
    if operator_id != post_author_id:
        return "not_owner"
    if board not in ("qa", "bounty"):
        return "not_board"
    if board == "bounty" and comment_author_id == post_author_id:
        return "self_response"
    if board == "bounty" and already_accepted:
        return "already_settled"
    return None


def decide_accept_score(post_author_id: int, comment_author_id: int) -> int:
    """首次采纳计分（纯函数）：自问自答不计分。"""
    if comment_author_id == post_author_id:
        return 0
    return SCORE_ANSWER_ACCEPTED
