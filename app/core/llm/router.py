"""模型分级调用路由（§10.2）：短问题 8K 档 / 章节级 32K 档 / 跨章节 128K 档。"""

import enum

from app.config import get_settings
from app.core.llm.client import LLMClient


class ModelTier(enum.Enum):
    SHORT = "short"  # 单知识点问答、审核初评、摘要等批量任务
    MEDIUM = "medium"  # 章节总结 / 复习大纲
    LONG = "long"  # 跨章节对比 / 全书框架


def get_official_client(tier: ModelTier = ModelTier.SHORT) -> LLMClient:
    """官方模型模式：平台统一直连低价 API（默认 DeepSeek），走配额制。"""
    settings = get_settings()
    model = {
        ModelTier.SHORT: settings.official_model_short,
        ModelTier.MEDIUM: settings.official_model_medium,
        ModelTier.LONG: settings.official_model_long,
    }[tier]
    return LLMClient(settings.official_llm_base_url, settings.official_llm_api_key, model)


def get_user_client(user_id: int, tier: ModelTier = ModelTier.SHORT) -> LLMClient | None:
    """用户自定义 Key 模式：user_llm_keys 存在记录则返回专属客户端，否则 None。"""
    # TODO(v0.5): 读库 + Fernet 解密（identity/quota 模块配合）
    return None
