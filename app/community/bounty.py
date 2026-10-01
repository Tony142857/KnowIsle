"""资料求援（模块 B3，v0.7）：悬赏贡献分 → 响应者评论响应 → 帖主采纳结算赏金。

积分流转（Owner 已确认的落地形态）：
- 发帖时托管：bounty_score 从帖主贡献分中扣减（score_logs 记 bounty_escrow），
  帖子保持 open 状态期间赏金由平台托管；
- 响应：任何用户（帖主除外）可在求援帖下评论响应（可附已上传的资源链接），
  评论即触发「悬赏被响应」站内通知（§B5 事件源）；
- 结算：帖主采纳某条响应评论时，托管赏金全额转给响应者（score_logs 记
  bounty_award）并双方通知；未采纳不结算、不退款（帖子保持 open）。
- 响应者另行上传的资料走常规三级审核，过审得 upload_approved +20（独立链路）。

零新增表：全部复用 posts.bounty_score / comments / score_logs / notifications。
"""

BOUNTY_MIN = 1  # 单次悬赏下限（贡献分）
BOUNTY_MAX = 1000  # 单次悬赏上限（贡献分，防误操作大额托管）

SCORE_BOUNTY_ESCROW = "bounty_escrow"  # 发帖托管（帖主 -N）
SCORE_BOUNTY_AWARD = "bounty_award"  # 采纳结算（响应者 +N）


def validate_bounty_score(score: int) -> None:
    """悬赏分校验（纯函数）：必须在 [BOUNTY_MIN, BOUNTY_MAX] 区间内。"""
    if score < BOUNTY_MIN or score > BOUNTY_MAX:
        raise ValueError(f"悬赏贡献分须在 {BOUNTY_MIN}~{BOUNTY_MAX} 之间")


def decide_bounty_award(post_author_id: int, responder_id: int, bounty_score: int) -> int:
    """采纳结算金额（纯函数）：自己响应自己的求援不结算（与自问自答不计分同口径）。"""
    if post_author_id == responder_id:
        return 0
    return bounty_score
