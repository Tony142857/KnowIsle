"""ARQ 连接池（§6.1）：模块级缓存，API 层任务入队共用。"""

from arq.connections import ArqRedis, RedisSettings, create_pool

from app.config import get_settings

_arq_pool: ArqRedis | None = None


async def get_arq_pool() -> ArqRedis:
    """模块级缓存的 ARQ 连接池（解析 / 审核 / 预览 / 摘要任务入队用）。"""
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(
            RedisSettings.from_dsn(get_settings().redis_url)
        )
    return _arq_pool
