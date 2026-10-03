"""seed_demo 演示社区内容种子测试（纯数据 / 纯函数，无 DB 依赖，同 fakestack 约定）。

覆盖：幂等判重键（标题 + 作者）唯一性、四板块数量与课程覆盖、经验帖四节模板、
AI 预填的状态推导（done 而非 pending/failed）、赏金/采纳/精华计分事件配平
（plan_score_effects 与 seed_posts 写库共用同一口径）。
"""

from collections import Counter

import pytest

from app.community.bounty import SCORE_BOUNTY_ESCROW, validate_bounty_board, validate_bounty_score
from app.community.comments import SCORE_ANSWER_ACCEPTED
from app.community.experience import (
    EXPERIENCE_TEMPLATE_SECTIONS,
    SCORE_POST_FEATURED,
    SECTION_HEADER_PREFIX,
)
from app.community.posts import (
    derive_ai_answer_status,
    derive_summary_status,
    validate_board,
    validate_tags,
)
from scripts.seed_demo import (
    AI_ANSWER_DISCLAIMER,
    DEMO_COMMENTS,
    DEMO_COURSES,
    DEMO_POSTS,
    DEMO_USERS,
    plan_score_effects,
)

_QA_MIN = 3
_DISCUSS_MIN = 2
_EXPERIENCE_MIN = 3
_BOUNTY_MIN = 1


def _posts_by_title() -> dict[str, dict]:
    return {p["title"]: p for p in DEMO_POSTS}


def _by_board(board: str) -> list[dict]:
    return [p for p in DEMO_POSTS if p["board"] == board]


# ---------------------------------------------------------------------------
# 幂等判重键与板块覆盖
# ---------------------------------------------------------------------------


def test_titles_unique_as_idempotency_key():
    titles = [p["title"] for p in DEMO_POSTS]
    assert len(titles) == len(set(titles)), "种子标题存在重复，判重键失效"
    keys = [(p["title"], p["author"]) for p in DEMO_POSTS]
    assert len(keys) == len(set(keys))


def test_board_coverage_and_course_binding():
    counts = Counter(p["board"] for p in DEMO_POSTS)
    assert counts["qa"] >= _QA_MIN
    assert counts["discuss"] >= _DISCUSS_MIN
    assert counts["experience"] >= _EXPERIENCE_MIN
    assert counts["bounty"] >= _BOUNTY_MIN
    # 每门示例公共课程恰好 1 帖 qa（三门全覆盖）
    qa_courses = {p["course"] for p in _by_board("qa")}
    assert qa_courses == {c["name"] for c in DEMO_COURSES}


def test_board_and_tags_pass_online_validation():
    authors = {u["student_no"] for u in DEMO_USERS}
    for spec in DEMO_POSTS:
        validate_board(spec["board"])
        validate_tags(spec.get("tags"))
        validate_bounty_board(spec["board"], spec.get("bounty_score", 0))
        assert spec["author"] in authors
    # qa 与经验帖必须带标签（标签云/过滤要有内容）
    for spec in _by_board("qa") + _by_board("experience"):
        assert spec.get("tags"), f"帖子「{spec['title']}」缺少标签"
    # 标签数足够撑起标签云
    all_tags = {t for p in _by_board("experience") for t in p["tags"]}
    assert len(all_tags) >= 5


# ---------------------------------------------------------------------------
# 经验帖结构化模板与作者届别
# ---------------------------------------------------------------------------


def test_experience_content_follows_four_section_template():
    for spec in _by_board("experience"):
        content = spec["content"]
        positions = []
        for name in EXPERIENCE_TEMPLATE_SECTIONS:
            header = f"{SECTION_HEADER_PREFIX}{name}\n"
            assert header in content, f"「{spec['title']}」缺少小节 {name}"
            positions.append(content.index(header))
        assert positions == sorted(positions), "小节顺序不符合模板"


def test_experience_authors_have_distinct_grades():
    grades = {u["student_no"]: u["grade"] for u in DEMO_USERS}
    exp_grades = [grades[p["author"]] for p in _by_board("experience")]
    assert len(set(exp_grades)) == len(exp_grades) >= 3, "经验帖作者年级应各异（届别过滤演示）"


# ---------------------------------------------------------------------------
# AI 内容预填与状态推导
# ---------------------------------------------------------------------------


def test_qa_ai_first_answer_prefilled_and_derives_done():
    for spec in _by_board("qa"):
        answer = spec.get("ai_first_answer")
        assert answer and answer.startswith(AI_ANSWER_DISCLAIMER)
        # DB 字段非空即 done，即使 Redis 残留 pending/failed 键也以 DB 为准
        assert derive_ai_answer_status(spec["board"], answer, None) == "done"
        assert derive_ai_answer_status(spec["board"], answer, "pending") == "done"
        assert derive_ai_answer_status(spec["board"], answer, "failed") == "done"


def test_experience_ai_summary_prefilled_and_derives_done():
    for spec in _by_board("experience"):
        summary = spec.get("ai_summary")
        assert summary and len(summary) >= 30
        assert derive_summary_status(spec["board"], summary, None) == "done"
        assert derive_summary_status(spec["board"], summary, "pending") == "done"


def test_ai_fields_only_on_their_boards():
    for spec in DEMO_POSTS:
        if spec["board"] != "qa":
            assert spec.get("ai_first_answer") is None
        if spec["board"] != "experience":
            assert spec.get("ai_summary") is None


# ---------------------------------------------------------------------------
# 评论结构（每 qa 帖 2~4 条、含楼中楼、恰好 1 条采纳、采纳非自问自答）
# ---------------------------------------------------------------------------


def test_qa_comments_structure():
    posts = _posts_by_title()
    authors = {u["student_no"] for u in DEMO_USERS}
    for spec in _by_board("qa"):
        comments = DEMO_COMMENTS.get(spec["title"])
        assert comments is not None, f"qa 帖「{spec['title']}」缺少评论定义"
        assert 2 <= len(comments) <= 4
        accepted = [c for c in comments if c.get("accepted")]
        assert len(accepted) == 1, "每 qa 帖恰好 1 条采纳"
        assert accepted[0]["author"] != spec["author"], "自问自答不计分，种子须避开"
        parents = [i for i, c in enumerate(comments) if c.get("parent") is not None]
        assert parents, "每 qa 帖至少 1 条楼中楼"
        for idx, c in enumerate(comments):
            assert c["author"] in authors
            if c.get("parent") is not None:
                assert 0 <= c["parent"] < idx, "楼中楼 parent 必须指向更早的评论"
    # 评论键必须落在种子帖标题内
    assert set(DEMO_COMMENTS) <= set(posts)


def test_no_self_votes():
    for spec in DEMO_POSTS:
        for voter_no, value in spec.get("votes", []):
            assert voter_no != spec["author"]
            assert value in (1, -1)
    for comments in DEMO_COMMENTS.values():
        for c in comments:
            for voter_no in c.get("votes", []):
                assert voter_no != c["author"], "不能给自己的评论点赞"


# ---------------------------------------------------------------------------
# 计分事件配平（users.score 与 score_logs 双写口径的纯函数镜像）
# ---------------------------------------------------------------------------


def test_bounty_escrow_matches_bounty_score():
    validate_bounty_score(30)
    bounty_posts = _by_board("bounty")
    effects = [e for e in plan_score_effects() if e["reason"] == SCORE_BOUNTY_ESCROW]
    assert len(effects) == len(bounty_posts) >= 1
    for post, effect in zip(bounty_posts, effects, strict=True):
        assert effect["delta"] == -post["bounty_score"]
        assert effect["student_no"] == post["author"]
        assert effect["ref_type"] == "post"


def test_featured_effect_once_and_not_self_marked():
    effects = [e for e in plan_score_effects() if e["reason"] == "post_featured"]
    assert len(effects) == 1
    featured_posts = [p for p in DEMO_POSTS if p.get("status") == "featured"]
    assert len(featured_posts) == 1
    assert featured_posts[0]["board"] == "experience"
    assert effects[0]["delta"] == SCORE_POST_FEATURED
    assert effects[0]["student_no"] == featured_posts[0]["author"]


def test_accept_effects_match_qa_posts():
    effects = [e for e in plan_score_effects() if e["reason"] == "answer_accepted"]
    assert len(effects) == len(_by_board("qa"))
    posts = _posts_by_title()
    for effect in effects:
        assert effect["delta"] == SCORE_ANSWER_ACCEPTED
        title, idx = effect["ref_key"]
        assert effect["ref_type"] == "comment"
        assert effect["student_no"] != posts[title]["author"], "采纳计分不得自问自答"
        assert DEMO_COMMENTS[title][idx].get("accepted")


def test_score_effects_net_per_user():
    """全量配平：每个演示账号的计分净额（即种子后 users.score 终值，初始 0）。"""
    totals: Counter[str] = Counter()
    for effect in plan_score_effects():
        totals[effect["student_no"]] += effect["delta"]
    # 张三：精华 +30、qa3 回答被采纳 +15、悬赏托管 -30 → +15
    # 李四：qa1 回答被采纳 +15；王五：qa2 回答被采纳 +15；赵六：无计分事件
    assert totals == Counter({"20260001": 15, "20260101": 15, "20260102": 15})
    # 悬赏帖作者净额非负（真实口径下发帖托管要求余额充足）
    bounty_author = _by_board("bounty")[0]["author"]
    assert totals[bounty_author] >= 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
