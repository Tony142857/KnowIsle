"""v0.5 社区互动单元测试（§18.1）：板块/标签校验、投票状态机、采纳资格、AI 首答状态推导。

与 test_identity.py / test_moderation.py 同一约定：CI 无 DB/Redis/Chroma/LLM，
全部为纯函数与 pydantic 模型校验的内存测试；全链路在容器 compose 环境内验证。
"""

import pytest
from pydantic import ValidationError

from app.api.posts import CreateCommentRequest, CreatePostRequest
from app.api.votes import VoteRequest
from app.community.comments import (
    SCORE_ANSWER_ACCEPTED,
    decide_accept_score,
    validate_accept,
)
from app.community.posts import (
    ai_answer_key,
    derive_ai_answer_status,
    make_excerpt,
    validate_board,
    validate_tags,
)
from app.community.votes import decide_vote

# ---------------------------------------------------------------------------
# 发帖参数校验（community/posts.py）
# ---------------------------------------------------------------------------


def test_validate_board_open():
    assert validate_board("qa") is None
    assert validate_board("discuss") is None


def test_validate_board_closed_boards():
    # experience / bounty 为 v0.7 板块，提示「板块暂未开放」
    for board in ("experience", "bounty"):
        with pytest.raises(ValueError, match="板块暂未开放"):
            validate_board(board)


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


def test_ai_answer_key():
    assert ai_answer_key(42) == "knowisle:ai_answer:42"


def test_create_post_request_validation():
    req = CreatePostRequest(board="qa", title="如何复习操作系统？", content="正文", course_id=1)
    assert req.tags is None and req.chapter_id is None
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
    assert validate_accept("qa", 1, 1, 9, 9) is None


def test_validate_accept_not_owner():
    # 非帖主：404 语义
    assert validate_accept("qa", 1, 2, 9, 9) == "not_owner"


def test_validate_accept_not_qa():
    assert validate_accept("discuss", 1, 1, 9, 9) == "not_qa"


def test_validate_accept_wrong_post():
    assert validate_accept("qa", 1, 1, 8, 9) == "wrong_post"


def test_decide_accept_score():
    # 自问自答不计分；他人回答 +15
    assert decide_accept_score(post_author_id=1, comment_author_id=1) == 0
    assert decide_accept_score(post_author_id=1, comment_author_id=2) == SCORE_ANSWER_ACCEPTED == 15
