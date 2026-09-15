"""Chroma 向量库封装（§5.4）：双 Collection 物理隔离。

通过 HTTP API 访问 compose 中的 chroma 服务，避免在应用镜像中引入
完整 chromadb 服务端依赖。Embedding 由 app/core/embeddings 注入。
"""

import httpx

from app.config import get_settings

COLLECTION_PUBLIC = "chunks_public"
COLLECTION_PERSONAL = "chunks_personal"


class VectorStore:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or get_settings().chroma_url).rstrip("/")

    async def heartbeat(self) -> bool:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=5) as client:
            resp = await client.get("/api/v2/heartbeat")
            return resp.status_code == 200

    # TODO(v0.3): get_or_create_collection / upsert / query（带 scope+course 双层过滤）


def get_vector_store() -> VectorStore:
    return VectorStore()
