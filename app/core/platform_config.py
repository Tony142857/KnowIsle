"""平台配置（模块 B6，v0.6）：少量运营参数支持管理后台在线调整。

生效规则：platform_config 表（第 26 表）有记录则用记录值，否则回落
.env / Settings 默认值；每次修改由 admin 接口写 audit_logs（config_change）。
仅 CONFIG_SPECS 注册的键可调整，类型与取值范围由 spec 校验，未知键拒绝。

当前可调键（全部整数）：
- ai_daily_limit            每用户每日 AI 问答免费额度（默认 settings.ai_daily_free_quota）
- ai_quota_exchange_rate    贡献分兑换 1 次额外额度的分值（默认 settings.ai_quota_exchange_rate）
- review_co_timeout_hours   协审超时自动重指派时限（默认 settings.review_co_timeout_hours）
- credit_mute_days          信用分 <60 自动禁言天数（默认 settings.credit_mute_days，v0.8）
- credit_rate_limit_cooldown_seconds  信用分 <80 发帖/评论/上传冷却秒数
  （默认 settings.credit_rate_limit_cooldown_seconds，v0.8）
"""

from dataclasses import dataclass
from time import monotonic

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.storage.models import PlatformConfig

# 进程内读缓存（v0.9）：get_config 调用点遍布发帖/评论/上传/问答，避免每请求一次 SELECT。
# 键数受 CONFIG_SPECS 限制（个位数），无内存风险；TTL 兜底多实例间的陈旧窗口，
# 本进程内 set_config 立即失效（见下），保证写后读一致。
_CONFIG_CACHE_TTL = 30.0  # 秒
_config_cache: dict[str, tuple[float, int]] = {}


@dataclass(frozen=True)
class ConfigSpec:
    key: str
    default: int
    description: str
    min_value: int
    max_value: int


def config_specs() -> dict[str, ConfigSpec]:
    """可调配置注册表（default 取自当前 Settings，.env 为兜底默认）。"""
    s = get_settings()
    return {
        "ai_daily_limit": ConfigSpec(
            key="ai_daily_limit",
            default=s.ai_daily_free_quota,
            description="每用户每日 AI 问答免费额度",
            min_value=0,
            max_value=10000,
        ),
        "ai_quota_exchange_rate": ConfigSpec(
            key="ai_quota_exchange_rate",
            default=s.ai_quota_exchange_rate,
            description="贡献分兑换 1 次 AI 额度的分值",
            min_value=1,
            max_value=1000,
        ),
        "review_co_timeout_hours": ConfigSpec(
            key="review_co_timeout_hours",
            default=s.review_co_timeout_hours,
            description="协审超时自动重指派时限（小时）",
            min_value=1,
            max_value=720,
        ),
        "credit_mute_days": ConfigSpec(
            key="credit_mute_days",
            default=s.credit_mute_days,
            description="信用分低于 60 自动禁言天数",
            min_value=1,
            max_value=30,
        ),
        "credit_rate_limit_cooldown_seconds": ConfigSpec(
            key="credit_rate_limit_cooldown_seconds",
            default=s.credit_rate_limit_cooldown_seconds,
            description="信用分低于 80 发帖/评论/上传冷却秒数",
            min_value=30,
            max_value=3600,
        ),
    }


def validate_value(key: str, raw: object) -> int:
    """校验并规整配置值（纯函数）。

    ValueError 约定：unknown_key（未注册键）→ 404；bad_type（非整数）/
    out_of_range（越界）→ 422。注意 bool 是 int 子类，须显式拒绝。
    """
    spec = config_specs().get(key)
    if spec is None:
        raise ValueError("unknown_key")
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError("bad_type")
    if not spec.min_value <= raw <= spec.max_value:
        raise ValueError("out_of_range")
    return raw


async def get_config(session: AsyncSession, key: str) -> int:
    """读生效配置：DB 覆盖优先，无记录回落 .env 默认值。未知键抛 KeyError（编程错误）。

    命中进程内缓存（TTL 30s）时不查库；set_config 写入会立即失效对应键。
    """
    spec = config_specs()[key]
    now = monotonic()
    hit = _config_cache.get(key)
    if hit is not None and now - hit[0] < _CONFIG_CACHE_TTL:
        return hit[1]
    row = (
        await session.execute(
            select(PlatformConfig.value).where(PlatformConfig.key == key)
        )
    ).scalar_one_or_none()
    value = spec.default if row is None else int(row)
    _config_cache[key] = (now, value)
    return value


async def set_config(
    session: AsyncSession, key: str, raw: object, admin_id: int
) -> tuple[int | None, int]:
    """写配置覆盖（upsert），返回 (旧值或 None, 新值)；调用方负责 commit 与审计。

    校验失败抛 ValueError（约定同 validate_value）。
    """
    new_value = validate_value(key, raw)
    row = (
        await session.execute(
            select(PlatformConfig).where(PlatformConfig.key == key)
        )
    ).scalar_one_or_none()
    old_value = None if row is None else int(row.value)
    if row is None:
        session.add(PlatformConfig(key=key, value=new_value, updated_by=admin_id))
    else:
        row.value = new_value
        row.updated_by = admin_id
    # 失效读缓存：写入后立即生效（下次 get_config 回源；同事务 SELECT 会 autoflush 到新值）
    _config_cache.pop(key, None)
    return old_value, new_value
