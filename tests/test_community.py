"""v0.5/v0.7 社区互动单元测试（§18.1）：板块/标签校验、投票状态机、采纳资格、
AI 首答与经验帖 AI 摘要状态推导、资料求援悬赏流转、经验长廊结构化模板与精华标记。

与 test_identity.py / test_moderation.py 同一约定：CI 无 DB/Redis/Chroma/LLM，
全部为纯函数与 pydantic 模型校验的内存测试；全链路在容器 compose 环境内验证。
"""

import pytest
from pydantic import ValidationError

from app.api.posts import CreateCommentRequest, CreatePostRequest
from app.api.votes import VoteRequest
from app.community.bounty import (
    decide_bounty_award,
    validate_bounty_board,
    validate_bounty_score,
)
from app.community.comments import (
    SCORE_ANSWER_ACCEPTED,
    decide_accept_score,
    validate_accept,
)
from app.community.experience import (
    SCORE_POST_FEATURED,
    build_experience_content,
    can_feature,
    validate_feature_target,
)
from app.community.posts import (
    ai_answer_key,
    derive_ai_answer_status,
    derive_summary_status,
    exp_summary_key,
    make_excerpt,
    validate_board,
    validate_tags,
)
from app.community.votes import decide_vote

# ---------------------------------------------------------------------------
# 发帖参数校验（community/posts.py）
# ---------------------------------------------------------------------------


def test_validate_board_open():
    # v0.7 起四板块全部开放
    for board in ("qa", "discuss", "experience", "bounty"):
        assert validate_board(board) is None


def test_validate_board_unknown():
    with pytest.raises(ValueError, match="未知板块"):
        validate_board("not-a-board")


def test_validate_tags_ok():
    assert validate_tags(None) == []
    assert validate_tags(["保研", "夏令营"]) == ["保研", "夏令营"]
    assert validate_tags(["a", "a", "b"]) == ["a", "b"]  # 去重保持顺序


def test_validate_tags_too_many():
    with pytest.raises(ValueError, match="最多"):
        validate_tags(["t1", "t2", "t3", "t4", "t5", "t6"])


def test_validate_tags_too_long():
    with pytest.raises(ValueError, match="长度"):
        validate_tags(["x" * 21])
    with pytest.raises(ValueError, match="长度"):
        validate_tags([""])


def test_make_excerpt():
    assert make_excerpt("短内容") == "短内容"
    assert len(make_excerpt("字" * 200)) == 120


def test_make_excerpt_strips_markdown():
    """v1.0：列表摘要剥离 Markdown 标记（经验帖四节模板的 ## 不再外露）。"""
    md = "## 背景\n2023 级**计算机**专业，排名 9/102。\n## 时间线\n大三上稳住 GPA。"
    excerpt = make_excerpt(md)
    assert "##" not in excerpt and "**" not in excerpt
    assert excerpt.startswith("背景 2023 级计算机专业")
    assert make_excerpt("参见[文档](https://example.com)第 3 节") == "参见文档第 3 节"


def test_ai_answer_key():
    assert ai_answer_key(42) == "knowisle:ai_answer:42"


def test_create_post_request_validation():
    req = CreatePostRequest(board="qa", title="如何复习操作系统？", content="正文", course_id=1)
    assert req.tags is None and req.chapter_id is None and req.bounty_score == 0
    # 求援帖可带悬赏分
    assert CreatePostRequest(board="bounty", title="求资料", content="正文", bounty_score=50
                             ).bounty_score == 50
    with pytest.raises(ValidationError):
        CreatePostRequest(board="qa", title="", content="正文")
    with pytest.raises(ValidationError):
        CreatePostRequest(board="qa", title="标题", content="")


def test_create_comment_request_validation():
    assert CreateCommentRequest(content="顶").parent_id is None
    with pytest.raises(ValidationError):
        CreateCommentRequest(content="")


# ---------------------------------------------------------------------------
# AI 首答状态推导（community/posts.py）
# ---------------------------------------------------------------------------


def test_derive_ai_answer_status_non_qa():
    # 非 qa 帖恒为 none（即使误写了 ai_first_answer）
    assert derive_ai_answer_status("discuss", None, "pending") == "none"
    assert derive_ai_answer_status("discuss", "回答", None) == "none"


def test_derive_ai_answer_status_done():
    assert derive_ai_answer_status("qa", "AI 回答", "pending") == "done"


def test_derive_ai_answer_status_redis_states():
    assert derive_ai_answer_status("qa", None, "pending") == "pending"
    assert derive_ai_answer_status("qa", None, "failed") == "failed"
    assert derive_ai_answer_status("qa", None, None) == "none"
    assert derive_ai_answer_status("qa", None, "garbage") == "none"


# ---------------------------------------------------------------------------
# 经验帖 AI 摘要状态推导（community/posts.py，v0.7）
# ---------------------------------------------------------------------------


def test_exp_summary_key():
    assert exp_summary_key(7) == "knowisle:exp_summary:7"


def test_derive_summary_status_non_experience():
    # 非经验帖恒为 none（即使误写了 ai_summary）
    assert derive_summary_status("qa", None, "pending") == "none"
    assert derive_summary_status("discuss", "摘要", None) == "none"


def test_derive_summary_status_done():
    assert derive_summary_status("experience", "摘要", "pending") == "done"


def test_derive_summary_status_redis_states():
    assert derive_summary_status("experience", None, "pending") == "pending"
    assert derive_summary_status("experience", None, "failed") == "failed"
    assert derive_summary_status("experience", None, None) == "none"
    assert derive_summary_status("experience", None, "garbage") == "none"


# ---------------------------------------------------------------------------
# 资料求援：悬赏校验与结算（community/bounty.py，v0.7）
# ---------------------------------------------------------------------------


def test_validate_bounty_score_ok():
    validate_bounty_score(1)
    validate_bounty_score(500)
    validate_bounty_score(1000)


def test_validate_bounty_score_out_of_range():
    with pytest.raises(ValueError, match="悬赏贡献分"):
        validate_bounty_score(0)
    with pytest.raises(ValueError, match="悬赏贡献分"):
        validate_bounty_score(1001)


def test_validate_bounty_board_rejects_non_bounty():
    # 非求援板块带悬赏分：报错而非静默忽略
    with pytest.raises(ValueError, match="仅资料求援板块支持悬赏"):
        validate_bounty_board("qa", 50)
    with pytest.raises(ValueError, match="仅资料求援板块支持悬赏"):
        validate_bounty_board("experience", 1)


def test_validate_bounty_board_ok():
    validate_bounty_board("bounty", 50)  # 求援板块带分：正常
    validate_bounty_board("qa", 0)  # 非求援板块 0 分：通过
    validate_bounty_board("discuss", 0)


def test_decide_bounty_award():
    # 他人响应：全额赏金；自己响应自己：不结算（与自问自答不计分同口径）
    assert decide_bounty_award(post_author_id=1, responder_id=2, bounty_score=50) == 50
    assert decide_bounty_award(post_author_id=1, responder_id=1, bounty_score=50) == 0


# ---------------------------------------------------------------------------
# 经验长廊：结构化模板与精华标记（community/experience.py，v0.7）
# ---------------------------------------------------------------------------


def test_build_experience_content():
    content = build_experience_content(
        {"背景": "计算机专业，排名前 10%", "时间线": "3 月报名，5 月入营",
         "经验要点": "", "避坑提示": "  "}
    )
    # 空小节不入正文，非空小节按固定顺序输出「## 小节 + 内容」
    assert content == "## 背景\n计算机专业，排名前 10%\n\n## 时间线\n3 月报名，5 月入营"


def test_build_experience_content_all_empty():
    assert build_experience_content({"背景": "", "时间线": "", "经验要点": "", "避坑提示": ""}) == ""


class _FakeUser:
    """can_feature 只需要 role 与 id 字段（纯函数，内存替身）。"""

    def __init__(self, role: str, user_id: int = 2):
        self.role = role
        self.id = user_id


def test_can_feature():
    # 管理员/共建者可标记他人的帖子
    assert can_feature(_FakeUser("admin"), post_author_id=1) is True
    assert can_feature(_FakeUser("builder"), post_author_id=1) is True
    # 帖主本人不可标记（防刷分）
    assert can_feature(_FakeUser("admin", user_id=1), post_author_id=1) is False
    assert can_feature(_FakeUser("builder", user_id=1), post_author_id=1) is False
    # 其余角色一律不可
    assert can_feature(_FakeUser("reviewer"), post_author_id=1) is False
    assert can_feature(_FakeUser("student"), post_author_id=1) is False


def test_validate_feature_target():
    assert validate_feature_target("experience", post_author_id=1, operator_id=2) is None
    # 仅经验长廊支持精华标记
    assert validate_feature_target("qa", post_author_id=1, operator_id=2) == "wrong_board"
    # 不可标记自己的帖子（防刷分）
    assert validate_feature_target("experience", post_author_id=1, operator_id=1) == "self"


def test_feature_score_constant():
    assert SCORE_POST_FEATURED == 30  # §3.3.1：经验帖被评为精华 +30


# ---------------------------------------------------------------------------
# 投票状态机（community/votes.py）
# ---------------------------------------------------------------------------


def test_decide_vote_insert():
    assert decide_vote(None, 1) == ("insert", 1)
    assert decide_vote(None, -1) == ("insert", -1)


def test_decide_vote_same_value_cancels():
    assert decide_vote(1, 1) == ("delete", 0)
    assert decide_vote(-1, -1) == ("delete", 0)


def test_decide_vote_flip():
    assert decide_vote(1, -1) == ("update", -1)
    assert decide_vote(-1, 1) == ("update", 1)


def test_vote_request_literal():
    assert VoteRequest(target_type="post", target_id=1, value=1).value == 1
    with pytest.raises(ValidationError):
        VoteRequest(target_type="user", target_id=1, value=1)
    with pytest.raises(ValidationError):
        VoteRequest(target_type="post", target_id=1, value=2)


# ---------------------------------------------------------------------------
# 采纳资格与计分（community/comments.py）
# ---------------------------------------------------------------------------


def test_validate_accept_ok():
    # 帖主采纳他人评论：qa / bounty 均可（求援采纳 = 悬赏结算）
    assert validate_accept("qa", 1, 1, 2, False) is None
    assert validate_accept("bounty", 1, 1, 2, False) is None


def test_validate_accept_not_owner():
    # 非帖主：404 语义
    assert validate_accept("qa", 1, 2, 2, False) == "not_owner"


def test_validate_accept_not_board():
    # 仅问答贴/求援贴可采纳，其余板块 422 语义
    assert validate_accept("discuss", 1, 1, 2, False) == "not_board"
    assert validate_accept("experience", 1, 1, 2, False) == "not_board"


def test_validate_accept_bounty_self_response():
    # 求援帖不可采纳自己的响应（自响应不结算，端点映射 422）
    assert validate_accept("bounty", 1, 1, 1, False) == "self_response"


def test_validate_accept_bounty_already_settled():
    # 求援已结算后改采其他评论：拦截（端点映射 422）
    assert validate_accept("bounty", 1, 1, 2, True) == "already_settled"


def test_validate_accept_bounty_repeat_same_comment():
    # 重复采纳同一条评论（already_accepted=False）：幂等分支前置，资格判定放行
    assert validate_accept("bounty", 1, 1, 2, False) is None


def test_validate_accept_qa_self_answer_ok():
    # qa 帖自问自答可采纳（0 分）
    assert validate_accept("qa", 1, 1, 1, False) is None


def test_validate_accept_qa_reaccept_ok():
    # qa 帖改采仍允许（只换标记不动积分）
    assert validate_accept("qa", 1, 1, 2, True) is None


def test_decide_accept_score():
    # 自问自答不计分；他人回答 +15
    assert decide_accept_score(post_author_id=1, comment_author_id=1) == 0
    assert decide_accept_score(post_author_id=1, comment_author_id=2) == SCORE_ANSWER_ACCEPTED == 15
