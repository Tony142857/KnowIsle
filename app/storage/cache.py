"""Redis 封装（§7.2）：会话 / 缓存 / ARQ 队列 / 贡献榜 Sorted Set / 限流计数。"""

import redis.asyncio as redis

from app.config import get_settings

_pool: redis.ConnectionPool = redis.ConnectionPool.from_url(
    get_settings().redis_url, decode_responses=True
)


def get_redis() -> redis.Redis:
    return redis.Redis(connection_pool=_pool)
