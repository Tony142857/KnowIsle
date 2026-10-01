"""Chroma 向量库封装（§5.4）：双 Collection 物理隔离。

通过 HTTP API 访问 compose 中的 chroma 服务，避免在应用镜像中引入
完整 chromadb 服务端依赖。Embedding 由 app/core/embeddings 注入。

v0.9 性能优化（A3）：httpx.AsyncClient 改为按 base_url 复用的模块级惰性单例
（每请求新建客户端的 TCP/TLS 握手开销消除）；collection name→id 进程内缓存
（避免每次检索多发一次 get_or_create 往返）；query 不再回传 documents
（消费方只用 id 回表 PG，payload 体积立减）。
"""

import httpx

from app.config import get_settings

COLLECTION_PUBLIC = "chunks_public"
COLLECTION_PERSONAL = "chunks_personal"

_COLLECTIONS_PATH = "/api/v2/tenants/default_tenant/databases/default_database/collections"

# 模块级惰性单例：按 base_url 复用 AsyncClient（连接池随进程生命周期存在，无需 lifespan 钩子）
_clients: dict[str, httpx.AsyncClient] = {}

# collection name → id 进程内缓存：集合由 get_or_create 保证存在，id 一经创建不变
_collection_ids: dict[str, str] = {}


def _get_client(base_url: str) -> httpx.AsyncClient:
    client = _clients.get(base_url)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(base_url=base_url, timeout=60.0)
        _clients[base_url] = client
    return client


class VectorStoreError(RuntimeError):
    """向量库调用失败（带集合名与响应上下文的可读异常）。"""


class VectorStore:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or get_settings().chroma_url).rstrip("/")

    @property
    def _client(self) -> httpx.AsyncClient:
        return _get_client(self.base_url)

    async def heartbeat(self) -> bool:
        resp = await self._client.get("/api/v2/heartbeat", timeout=5)
        return resp.status_code == 200

    @staticmethod
    def _check(resp: httpx.Response, action: str, collection: str = "") -> None:
        if resp.status_code >= 400:
            raise VectorStoreError(
                f"Chroma {action} 失败（collection={collection or '-'}）："
                f"HTTP {resp.status_code} {resp.text[:300]}"
            )

    async def get_or_create_collection(self, name: str) -> str:
        """按名获取（不存在则创建）集合，返回集合 ID（进程内缓存，命中不发请求）。"""
        cache_key = f"{self.base_url}:{name}"
        if cache_key in _collection_ids:
            return _collection_ids[cache_key]
        resp = await self._client.post(
            _COLLECTIONS_PATH, json={"name": name, "get_or_create": True}, timeout=10
        )
        self._check(resp, "get_or_create_collection", name)
        collection_id = resp.json()["id"]
        _collection_ids[cache_key] = collection_id
        return collection_id

    async def upsert(
        self,
        collection: str,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
        documents: list[str],
    ) -> None:
        """批量写入/覆盖向量。metadata 值仅允许 str/int/float/bool。"""
        resp = await self._client.post(
            f"{_COLLECTIONS_PATH}/{collection}/upsert",
            json={
                "ids": ids,
                "embeddings": embeddings,
                "metadatas": metadatas,
                "documents": documents,
            },
            timeout=60,
        )
        self._check(resp, "upsert", collection)

    async def query(
        self,
        collection: str,
        query_embedding: list[float],
        n_results: int,
        where: dict | None = None,
    ) -> list[dict]:
        """向量相似度查询，返回 [{id, distance, metadata}]（按距离升序）。

        include 不含 documents：消费方只用 id 回表 PG 取行（正文以 PG 为准），
        去掉后响应 payload 体积随正文大小线性下降。
        """
        payload: dict = {
            "query_embeddings": [query_embedding],
            "n_results": n_results,
            "include": ["metadatas", "distances"],
        }
        if where:
            payload["where"] = where
        resp = await self._client.post(
            f"{_COLLECTIONS_PATH}/{collection}/query", json=payload, timeout=30
        )
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
                }
            )
        return hits


def get_vector_store() -> VectorStore:
    return VectorStore()
