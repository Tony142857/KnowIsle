"""经验长廊（模块 B3，v0.7）：结构化模板发帖 + 标签体系 + 精华标记 + AI 摘要。

发帖使用结构化模板（§4.3：背景 → 时间线 → 经验要点 → 避坑提示），前端分四栏填写、
按固定小节标题组装为 Markdown 正文存储（列表摘要/AI 摘要/全文检索均直接可用）。
精华帖由管理员/共建者标记（§4.3），作者 +30 贡献分（一次性，取消精华不回扣）。
经验帖的 AI 摘要与检索走 PostgreSQL，不进入 RAG chunk 向量检索（§4.3 一期口径）。
"""

from app.storage.models import User

# 结构化模板四个固定小节（发帖页按此渲染表单，空小节不入正文）
EXPERIENCE_TEMPLATE_SECTIONS = ("背景", "时间线", "经验要点", "避坑提示")

# 经验长廊标签体系（§4.3 五大方向；发帖页作为快捷标签建议，不强制约束）
EXPERIENCE_TAGS = ("保研", "竞赛", "实习", "选课", "转专业")

SECTION_HEADER_PREFIX = "## "  # 小节标题前缀（Markdown 二级标题，与文档 §11.3 风格一致）

# 精华标记奖励（§3.3.1：经验帖被评为精华 +30，管理员标记触发）
SCORE_POST_FEATURED = 30

# 可标记精华的角色（§4.3：精华帖由管理员/共建者标记）
FEATURE_ROLES = ("builder", "admin")


def build_experience_content(sections: dict[str, str]) -> str:
    """按结构化模板组装经验帖正文（纯函数）：非空小节按固定顺序输出 `## 小节` + 内容。

    四个小节全空时返回空串（调用方据此 422）；保留用户换行，不引入列表符号，
    保证 AI 摘要提示词（§11.7）拿到的是干净的分节文本。
    """
    parts = []
    for name in EXPERIENCE_TEMPLATE_SECTIONS:
        body = (sections.get(name) or "").strip()
        if body:
            parts.append(f"{SECTION_HEADER_PREFIX}{name}\n{body}")
    return "\n\n".join(parts)


def can_feature(user: User, post_author_id: int) -> bool:
    """精华标记资格（纯函数）：管理员/共建者可标记他人帖子，帖主本人不可（防刷分，§3.3.1）。"""
    return user.role in FEATURE_ROLES and user.id != post_author_id


def validate_feature_target(board: str, post_author_id: int, operator_id: int) -> str | None:
    """精华标记目标校验（纯函数）：返回错误码，None 表示可标记。

    wrong_board → 仅经验长廊支持精华标记（其余板块无此机制）；
    self → 不可标记自己的帖子（防刷分）。
    """
    if board != "experience":
        return "wrong_board"
    if operator_id == post_author_id:
        return "self"
    return None
