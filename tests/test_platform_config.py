"""v0.6 平台配置单元测试（模块 B6）：配置值校验纯函数、spec 注册表默认值
与 Settings 一致性、信用限幅、管理请求模型契约。

与 test_social.py 同一约定：纯函数内存测试，DB 全链路在容器内验证。
"""

import pytest
from pydantic import ValidationError

from app.api.admin import ConfigUpdateRequest, CreditAdjustRequest, UpdateRoleRequest
from app.config import get_settings
from app.core.platform_config import config_specs, validate_value
from app.identity.growth import CREDIT_MAX, CREDIT_MIN, clamp_credit

# ---------------------------------------------------------------------------
# validate_value：类型 / 范围 / 未知键
# ---------------------------------------------------------------------------


def test_validate_value_ok():
    assert validate_value("ai_daily_limit", 50) == 50
    assert validate_value("ai_quota_exchange_rate", 1) == 1
    assert validate_value("review_co_timeout_hours", 48) == 48


def test_validate_value_boundary():
    assert validate_value("ai_daily_limit", 0) == 0
    assert validate_value("ai_daily_limit", 10000) == 10000
    with pytest.raises(ValueError, match="out_of_range"):
        validate_value("ai_daily_limit", -1)
    with pytest.raises(ValueError, match="out_of_range"):
        validate_value("ai_daily_limit", 10001)
    with pytest.raises(ValueError, match="out_of_range"):
        validate_value("ai_quota_exchange_rate", 0)


def test_validate_value_credit_mute_days():
    # v0.8：信用分 <60 自动禁言天数，1~30
    assert validate_value("credit_mute_days", 1) == 1
    assert validate_value("credit_mute_days", 7) == 7
    assert validate_value("credit_mute_days", 30) == 30
    for raw in (0, 31, True):
        with pytest.raises(ValueError):
            validate_value("credit_mute_days", raw)


def test_validate_value_credit_rate_limit_cooldown():
    # v0.8：信用分 <80 操作冷却秒数，30~3600
    assert validate_value("credit_rate_limit_cooldown_seconds", 30) == 30
    assert validate_value("credit_rate_limit_cooldown_seconds", 300) == 300
    assert validate_value("credit_rate_limit_cooldown_seconds", 3600) == 3600
    for raw in (29, 3601, False):
        with pytest.raises(ValueError):
            validate_value("credit_rate_limit_cooldown_seconds", raw)


def test_validate_value_bad_type():
    # bool 是 int 子类，必须显式拒绝；字符串/浮点同样拒绝
    for raw in (True, "30", 30.5, None, [1]):
        with pytest.raises(ValueError, match="bad_type"):
            validate_value("ai_daily_limit", raw)


def test_validate_value_unknown_key():
    with pytest.raises(ValueError, match="unknown_key"):
        validate_value("not_a_config", 1)


# ---------------------------------------------------------------------------
# spec 注册表与 Settings 默认值一致性（改 .env 默认值时两侧同步生效）
# ---------------------------------------------------------------------------


def test_config_specs_defaults_match_settings():
    specs = config_specs()
    s = get_settings()
    assert specs["ai_daily_limit"].default == s.ai_daily_free_quota
    assert specs["ai_quota_exchange_rate"].default == s.ai_quota_exchange_rate
    assert specs["review_co_timeout_hours"].default == s.review_co_timeout_hours
    assert specs["credit_mute_days"].default == s.credit_mute_days
    assert (
        specs["credit_rate_limit_cooldown_seconds"].default
        == s.credit_rate_limit_cooldown_seconds
    )


def test_config_specs_registry_shape():
    specs = config_specs()
    assert set(specs) == {
        "ai_daily_limit",
        "ai_quota_exchange_rate",
        "review_co_timeout_hours",
        "credit_mute_days",
        "credit_rate_limit_cooldown_seconds",
    }
    for key, spec in specs.items():
        assert spec.key == key
        assert spec.min_value <= spec.default <= spec.max_value
        assert spec.description


# ---------------------------------------------------------------------------
# 信用分限幅（identity/growth.py）
# ---------------------------------------------------------------------------


def test_clamp_credit():
    assert clamp_credit(100) == 100
    assert clamp_credit(120) == CREDIT_MAX
    assert clamp_credit(-5) == CREDIT_MIN
    assert clamp_credit(85) == 85


# ---------------------------------------------------------------------------
# 管理请求模型契约
# ---------------------------------------------------------------------------


def test_update_role_request_whitelist():
    for role in ("student", "reviewer", "builder", "admin"):
        assert UpdateRoleRequest(role=role).role == role
    with pytest.raises(ValidationError):
        UpdateRoleRequest(role="superadmin")


def test_credit_adjust_request_bounds():
    assert CreditAdjustRequest(delta=-20, reason="恶意灌水").delta == -20
    with pytest.raises(ValidationError):
        CreditAdjustRequest(delta=-101, reason="x")
    with pytest.raises(ValidationError):
        CreditAdjustRequest(delta=101, reason="x")
    with pytest.raises(ValidationError):
        CreditAdjustRequest(delta=-10, reason="")


def test_config_update_request():
    assert ConfigUpdateRequest(value=30).value == 30
    with pytest.raises(ValidationError):
        ConfigUpdateRequest(value="abc")
