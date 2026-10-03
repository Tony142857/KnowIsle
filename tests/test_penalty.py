"""v0.8 信用阶梯处罚与举报/搜索单元测试：阶梯评估边界、有效状态推导、
禁言门控提示文案、冷却键、举报请求模型契约、搜索纯函数。

与 test_growth.py 同一约定：纯函数内存测试，CI 无 DB/Redis/Chroma/LLM。
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.api.reports import CreateReportRequest, validate_self_report
from app.community.search import (
    EXCERPT_WIDTH,
    make_excerpt,
    validate_query,
    validate_search_type,
)
from app.identity.growth import (
    TIER_FREEZE,
    TIER_MUTE,
    TIER_RATE_LIMIT,
    effective_status,
    evaluate_credit_tier,
)
from app.identity.penalty import cooldown_key, mute_detail

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# evaluate_credit_tier：阶梯边界（<40 冻结 / <60 禁言 / <80 限流）
# ---------------------------------------------------------------------------


def test_evaluate_credit_tier_normal():
    assert evaluate_credit_tier(100) == "normal"
    assert evaluate_credit_tier(TIER_RATE_LIMIT) == "normal"  # 80 恰好在正常档
    assert evaluate_credit_tier(81) == "normal"


def test_evaluate_credit_tier_rate_limited():
    assert evaluate_credit_tier(79) == "rate_limited"
    assert evaluate_credit_tier(TIER_MUTE) == "rate_limited"  # 60 恰好仍在限流档


def test_evaluate_credit_tier_muted():
    assert evaluate_credit_tier(59) == "muted"
    assert evaluate_credit_tier(TIER_FREEZE) == "muted"  # 40 恰好仍在禁言档


def test_evaluate_credit_tier_frozen():
    assert evaluate_credit_tier(39) == "frozen"
    assert evaluate_credit_tier(0) == "frozen"


# ---------------------------------------------------------------------------
# effective_status：禁言到期视为 active（惰性解除由门控落库）
# ---------------------------------------------------------------------------


def test_effective_status_muted_expired():
    assert effective_status("muted", NOW - timedelta(seconds=1), NOW) == "active"


def test_effective_status_muted_not_expired():
    assert effective_status("muted", NOW + timedelta(days=1), NOW) == "muted"


def test_effective_status_muted_without_deadline():
    # muted_until 为 NULL（无期限）时保持 muted，由信用恢复解除
    assert effective_status("muted", None, NOW) == "muted"


def test_effective_status_passthrough():
    assert effective_status("active", None, NOW) == "active"
    assert effective_status("frozen", None, NOW) == "frozen"


# ---------------------------------------------------------------------------
# 门控辅助：提示文案与冷却键
# ---------------------------------------------------------------------------


def test_mute_detail_includes_deadline():
    detail = mute_detail(NOW)
    assert NOW.isoformat() in detail
    assert "禁言" in detail


def test_mute_detail_without_deadline():
    assert "禁言" in mute_detail(None)


def test_cooldown_key():
    assert cooldown_key("post", 42) == "knowisle:rl:post:42"
    assert cooldown_key("upload", 7) == "knowisle:rl:upload:7"


# ---------------------------------------------------------------------------
# 举报请求模型契约与自举报校验
# ---------------------------------------------------------------------------


def test_create_report_request_ok():
    for target_type in ("resource", "post", "comment", "user"):
        req = CreateReportRequest(target_type=target_type, target_id=1, reason="垃圾信息")
        assert req.target_type == target_type


def test_create_report_request_reason_length():
    with pytest.raises(ValidationError):
        CreateReportRequest(target_type="post", target_id=1, reason="")
    with pytest.raises(ValidationError):
        CreateReportRequest(target_type="post", target_id=1, reason="x" * 501)
    assert CreateReportRequest(
        target_type="post", target_id=1, reason="x" * 500
    ).reason == "x" * 500


def test_create_report_request_target_type_whitelist():
    with pytest.raises(ValidationError):
        CreateReportRequest(target_type="admin", target_id=1, reason="x")
    with pytest.raises(ValidationError):
        CreateReportRequest(target_type="course", target_id=1, reason="x")


def test_validate_self_report():
    with pytest.raises(ValueError, match="self_report"):
        validate_self_report("user", 5, 5)
    validate_self_report("user", 5, 6)  # 举报他人：通过
    validate_self_report("post", 5, 5)  # 非 user 目标无自举报概念


# ---------------------------------------------------------------------------
# 搜索纯函数：validate_query / validate_search_type / make_excerpt
# ---------------------------------------------------------------------------


def test_validate_query_strips():
    assert validate_query("  操作系统  ") == "操作系统"


def test_validate_query_empty():
    for q in ("", "   ", "\t\n"):
        with pytest.raises(ValueError, match="不能为空"):
            validate_query(q)


def test_validate_query_length():
    assert validate_query("x" * 100) == "x" * 100
    with pytest.raises(ValueError, match="最长"):
        validate_query("x" * 101)


def test_validate_search_type():
    for t in ("all", "post", "resource"):
        assert validate_search_type(t) == t
    with pytest.raises(ValueError, match="未知搜索类型"):
        validate_search_type("comment")


def test_make_excerpt_hit_centered():
    content = "a" * 100 + "关键词命中" + "b" * 100
    excerpt = make_excerpt(content, "关键词命中", width=20)
    # 命中片段完整保留并大致居中，首尾截断处加省略号
    assert "关键词命中" in excerpt
    assert excerpt.startswith("…") and excerpt.endswith("…")
    assert len(excerpt) == 20 + 2


def test_make_excerpt_no_hit_truncates():
    content = "x" * (EXCERPT_WIDTH + 50)
    assert make_excerpt(content, "不存在") == "x" * EXCERPT_WIDTH


def test_make_excerpt_short_content_passthrough():
    assert make_excerpt("短内容", "短") == "短内容"


def test_make_excerpt_case_insensitive():
    content = "Hello World " * 20
    excerpt = make_excerpt(content, "world", width=30)
    assert "World" in excerpt
