"""v0.6 收藏 / 关注单元测试（模块 B3）：目标可收藏/可关注校验与开关语义纯函数、
请求模型契约。

与 test_clone.py 同一约定：CI 无 DB/Redis，全部为纯函数内存测试；
PG 全链路在容器 compose 环境内验证。
"""

import pytest
from pydantic import ValidationError

from app.api.favorites import FavoriteRequest
from app.api.follows import FollowRequest
from app.community.social import (
    course_followable,
    decide_social_write,
    post_favoritable,
    resource_favoritable,
    user_followable,
)

# ---------------------------------------------------------------------------
# 收藏目标校验
# ---------------------------------------------------------------------------


def test_resource_favoritable_only_approved():
    assert resource_favoritable("approved") is True
    for status in ("pending", "co_reviewing", "rejected", None):
        assert resource_favoritable(status) is False


def test_post_favoritable_any_existing():
    for status in ("normal", "featured", "closed"):
        assert post_favoritable(status) is True
    assert post_favoritable(None) is False


# ---------------------------------------------------------------------------
# 关注目标校验
# ---------------------------------------------------------------------------


def test_course_followable_public_active_only():
    assert course_followable("public", "active") is True
    assert course_followable("public", "pending") is False
    assert course_followable("public", "disabled") is False
    assert course_followable("personal", "active") is False
    assert course_followable(None, "active") is False


def test_user_followable_active_and_not_self():
    assert user_followable("active", is_self=False) is True
    assert user_followable("active", is_self=True) is False
    assert user_followable("frozen", is_self=False) is False
    assert user_followable("muted", is_self=False) is False
    assert user_followable(None, is_self=False) is False


# ---------------------------------------------------------------------------
# POST 幂等开关语义
# ---------------------------------------------------------------------------


def test_decide_social_write():
    assert decide_social_write(False) == "insert"
    assert decide_social_write(True) == "noop"


# ---------------------------------------------------------------------------
# 请求模型契约
# ---------------------------------------------------------------------------


def test_favorite_request_target_type_whitelist():
    assert FavoriteRequest(target_type="resource", target_id=1).target_type == "resource"
    assert FavoriteRequest(target_type="post", target_id=2).target_id == 2
    with pytest.raises(ValidationError):
        FavoriteRequest(target_type="course", target_id=1)


def test_follow_request_target_type_whitelist():
    assert FollowRequest(target_type="course", target_id=1).target_type == "course"
    assert FollowRequest(target_type="user", target_id=2).target_id == 2
    with pytest.raises(ValidationError):
        FollowRequest(target_type="post", target_id=1)
