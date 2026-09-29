"""官方接口 Embedding（可切换）：OpenAI 兼容 /embeddings 端点。

EMBEDDING_MODE=remote 时启用，走 EMBEDDING_BASE_URL / EMBEDDING_API_KEY。
"""

import httpx

from app.config import get_settings
from app.core.embeddings.base import BaseEmbedding


class RemoteEmbeddingError(RuntimeError):
    """远程 Embedding 接口调用失败（带状态码上下文）。"""


class RemoteEmbedding(BaseEmbedding):
    dim = 0  # 维度由远端模型决定，不在本地固化

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        settings = get_settings()
        base = settings.embedding_base_url.rstrip("/")
        async with httpx.AsyncClient(
            base_url=base, timeout=httpx.Timeout(connect=10, read=60, write=30, pool=10)
        ) as client:
            resp = await client.post(
                "/embeddings",
                headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
                json={"model": settings.embedding_model, "input": texts},
            )
        if resp.status_code != 200:
            raise RemoteEmbeddingError(
                f"Embedding 接口调用失败：HTTP {resp.status_code} {resp.text[:300]}"
            )
        data = resp.json()["data"]
        data.sort(key=lambda item: item["index"])
        return [[float(x) for x in item["embedding"]] for item in data]
