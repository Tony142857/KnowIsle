"""模型分级调用路由（§10.2）：短问题 8K 档 / 章节级 32K 档 / 跨章节 128K 档。

用户自定义 Key 优先于官方通道：model 选取规则为该 tier 对应字段 → model_short
→ 官方该 tier 模型名；解密失败（如 SECRET_KEY 轮换）降级官方通道并记日志，
不让用户卡死。
"""

import enum
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.llm.client import LLMClient
from app.identity.llm_keys import decrypt_api_key, get_user_key
from app.storage.models import User, UserLlmKey

logger = logging.getLogger(__name__)


class ModelTier(enum.Enum):
    SHORT = "short"  # 单知识点问答、审核初评、摘要等批量任务
    MEDIUM = "medium"  # 章节总结 / 复习大纲
    LONG = "long"  # 跨章节对比 / 全书框架


def _official_model(tier: ModelTier) -> str:
    settings = get_settings()
    return {
        ModelTier.SHORT: settings.official_model_short,
        ModelTier.MEDIUM: settings.official_model_medium,
        ModelTier.LONG: settings.official_model_long,
    }[tier]


def _pick_model(key: UserLlmKey, tier: ModelTier) -> str:
    """自定义 Key 的模型选取：该 tier 字段 → model_short → 官方该 tier 默认。"""
    tier_model = {
        ModelTier.SHORT: key.model_short,
        ModelTier.MEDIUM: key.model_medium,
        ModelTier.LONG: key.model_long,
    }[tier]
    return tier_model or key.model_short or _official_model(tier)


def get_official_client(tier: ModelTier = ModelTier.SHORT) -> LLMClient:
    """官方模型模式：平台统一直连低价 API（默认 DeepSeek），走配额制。"""
    settings = get_settings()
    return LLMClient(
        settings.official_llm_base_url, settings.official_llm_api_key, _official_model(tier)
    )


async def get_user_client(
    session: AsyncSession, user_id: int, tier: ModelTier = ModelTier.SHORT
) -> LLMClient | None:
    """用户自定义 Key 模式：user_llm_keys 存在记录则返回专属客户端，否则 None。

    密文解密失败（密钥轮换等）记日志并返回 None，调用方降级官方通道。
    """
    key = await get_user_key(session, user_id)
    if key is None:
        return None
    try:
        api_key = decrypt_api_key(key.api_key_enc)
    except Exception:
        logger.warning("user_id=%s 自定义 Key 解密失败，降级官方通道", user_id)
        return None
    return LLMClient(key.base_url, api_key, _pick_model(key, tier))


async def resolve_client(
    session: AsyncSession, user: User, tier: ModelTier = ModelTier.SHORT
) -> tuple[LLMClient, str]:
    """统一路由：优先用户自定义 Key，返回 (client, "user_custom" | "official")。"""
    client = await get_user_client(session, user.id, tier)
    if client is not None:
        return client, "user_custom"
    return get_official_client(tier), "official"
