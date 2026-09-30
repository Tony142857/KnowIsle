"""收藏 / 关注（模块 B3，v0.6）：目标可收藏/可关注校验与开关语义纯函数。

HTTP 层在 app/api/favorites.py 与 app/api/follows.py；
本模块只放可单测的纯判定逻辑（与 community/votes.py 同一约定）。
"""


def resource_favoritable(review_status: str | None) -> bool:
    """仅已上架（approved）资源可收藏；目标不存在时调用方先 404。"""
    return review_status == "approved"


def post_favoritable(status: str | None) -> bool:
    """帖子收藏不限制状态（closed 帖仍可收藏备查）；仅过滤不存在（None）。"""
    return status is not None


def course_followable(scope: str | None, status: str | None) -> bool:
    """仅 public + active 课程空间可关注（pending/disabled 不开放订阅）。"""
    return scope == "public" and status == "active"


def user_followable(status: str | None, is_self: bool) -> bool:
    """仅在职（active）且非本人的用户可关注。"""
    return status == "active" and not is_self


def decide_social_write(exists: bool) -> str:
    """POST 幂等开关语义（纯函数）：无记录 insert，已存在 noop。"""
    return "noop" if exists else "insert"
