"""评论服务（模块 B3）：楼中楼评论、采纳最佳答案（自问自答不计分）。"""

SCORE_ANSWER_ACCEPTED = 15  # 回答被采纳


def validate_accept(
    board: str, post_author_id: int, operator_id: int, comment_post_id: int, post_id: int
) -> str | None:
    """采纳资格判定（纯函数）：返回错误码，None 表示可采纳。

    not_owner → 404（越权不暴露存在性）；not_board / wrong_post → 422。
    v0.7 起问答贴与求援贴均可采纳（求援采纳 = 悬赏结算）。
    """
    if operator_id != post_author_id:
        return "not_owner"
    if board not in ("qa", "bounty"):
        return "not_board"
    if comment_post_id != post_id:
        return "wrong_post"
    return None


def decide_accept_score(post_author_id: int, comment_author_id: int) -> int:
    """首次采纳计分（纯函数）：自问自答不计分。"""
    if comment_author_id == post_author_id:
        return 0
    return SCORE_ANSWER_ACCEPTED
