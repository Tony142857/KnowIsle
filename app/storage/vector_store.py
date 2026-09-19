"""Chroma 向量库封装（§5.4）：双 Collection 物理隔离。

通过 HTTP API 访问 compose 中的 chroma 服务，避免在应用镜像中引入
完整 chromadb 服务端依赖。Embedding 由 app/core/embeddings 注入。
"""

import httpx

from app.config import get_settings

COLLECTION_PUBLIC = "chunks_public"
COLLECTION_PERSONAL = "chunks_personal"

_COLLECTIONS_PATH = "/api/v2/tenants/default_tenant/databases/default_database/collections"


class VectorStoreError(RuntimeError):
    """向量库调用失败（带集合名与响应上下文的可读异常）。"""


class VectorStore:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or get_settings().chroma_url).rstrip("/")

    async def heartbeat(self) -> bool:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=5) as client:
            resp = await client.get("/api/v2/heartbeat")
            return resp.status_code == 200

    @staticmethod
    def _check(resp: httpx.Response, action: str, collection: str = "") -> None:
        if resp.status_code >= 400:
            raise VectorStoreError(
                f"Chroma {action} 失败（collection={collection or '-'}）："
                f"HTTP {resp.status_code} {resp.text[:300]}"
            )

    async def get_or_create_collection(self, name: str) -> str:
        """按名获取（不存在则创建）集合，返回集合 ID。"""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as client:
            resp = await client.post(
                _COLLECTIONS_PATH, json={"name": name, "get_or_create": True}
            )
        self._check(resp, "get_or_create_collection", name)
        return resp.json()["id"]

    async def upsert(
        self,
        collection: str,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
        documents: list[str],
    ) -> None:
        """批量写入/覆盖向量。metadata 值仅允许 str/int/float/bool。"""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=60) as client:
            resp = await client.post(
                f"{_COLLECTIONS_PATH}/{collection}/upsert",
                json={
                    "ids": ids,
                    "embeddings": embeddings,
                    "metadatas": metadatas,
                    "documents": documents,
                },
            )
        self._check(resp, "upsert", collection)

    async def query(
        self,
        collection: str,
        query_embedding: list[float],
        n_results: int,
        where: dict | None = None,
    ) -> list[dict]:
        """向量相似度查询，返回 [{id, distance, metadata, document}]（按距离升序）。"""
        payload: dict = {
            "query_embeddings": [query_embedding],
            "n_results": n_results,
            "include": ["metadatas", "documents", "distances"],
        }
        if where:
            payload["where"] = where
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            resp = await client.post(f"{_COLLECTIONS_PATH}/{collection}/query", json=payload)
        self._check(resp, "query", collection)
        data = resp.json()
        hits = []
        # Chroma v2 响应按查询批次嵌套一层列表（本封装恒为单查询）
        for i, chunk_id in enumerate(data.get("ids", [[]])[0]):
            hits.append(
                {
                    "id": chunk_id,
                    "distance": data["distances"][0][i],
                    "metadata": data["metadatas"][0][i] or {},
                    "document": (data["documents"][0][i] or "") if data.get("documents") else "",
                }
            )
        return hits


def get_vector_store() -> VectorStore:
    return VectorStore()
