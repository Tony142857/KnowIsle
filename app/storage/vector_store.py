"""Chroma 向量库封装（§5.4）：双 Collection 物理隔离。

通过 HTTP API 访问 compose 中的 chroma 服务，避免在应用镜像中引入
完整 chromadb 服务端依赖。Embedding 由 app/core/embeddings 注入。

v0.9 性能优化（A3）：httpx.AsyncClient 改为按 base_url 复用的模块级惰性单例
（每请求新建客户端的 TCP/TLS 握手开销消除）；collection name→id 进程内缓存
（避免每次检索多发一次 get_or_create 往返）；query 不再回传 documents
（消费方只用 id 回表 PG，payload 体积立减）。

§20.5 B4（v1.1）：集合 id 进程缓存不再永不过期——upsert/query 失败且错误
表明集合不存在（向量库重建后 id 失效）时，清缓存重新解析 id 并重试一次。
"""

import httpx

from app.config import get_settings

COLLECTION_PUBLIC = "chunks_public"
COLLECTION_PERSONAL = "chunks_personal"

_COLLECTIONS_PATH = "/api/v2/tenants/default_tenant/databases/default_database/collections"

# 模块级惰性单例：按 base_url 复用 AsyncClient（连接池随进程生命周期存在，无需 lifespan 钩子）
_clients: dict[str, httpx.AsyncClient] = {}

# collection name → id 进程内缓存：命中不发请求；集合失效时由操作失败驱动清除重试（§20.5 B4）
_collection_ids: dict[str, str] = {}


def _get_client(base_url: str) -> httpx.AsyncClient:
    client = _clients.get(base_url)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(base_url=base_url, timeout=60.0)
        _clients[base_url] = client
    return client


def _is_collection_missing(resp: httpx.Response) -> bool:
    """响应是否表明集合不存在（id 缓存因向量库重建而失效）。"""
    return resp.status_code == 404 or "does not exist" in resp.text.lower()


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

    async def get_collection_metadata(self, name: str) -> dict:
        """读取当次真实索引版本/模型/状态，不使用应用配置冒充已建索引。

        Chroma 1.0.15 的集合详情 GET 只接受集合名（按 id 访问返回 404），
        count/get/delete 等子资源仍按 id 访问；此处固定用 name。
        """
        await self.get_or_create_collection(name)
        resp = await self._client.get(f"{_COLLECTIONS_PATH}/{name}", timeout=10)
        self._check(resp, "metadata", name)
        return resp.json().get("metadata") or {}

    def _evict_cached_id(self, collection_id: str) -> str | None:
        """按 id 反查并清除本实例的 name→id 进程缓存，返回集合名（无缓存返回 None）。"""
        prefix = f"{self.base_url}:"
        for cache_key, cached_id in list(_collection_ids.items()):
            if cached_id == collection_id and cache_key.startswith(prefix):
                del _collection_ids[cache_key]
                return cache_key[len(prefix):]
        return None

    async def _post(
        self, action: str, collection: str, suffix: str, payload: dict, timeout: float
    ) -> httpx.Response:
        """对集合发 POST；集合不存在（id 缓存失效）时清缓存重新解析 id 重试一次（§20.5 B4）。"""
        resp = await self._client.post(
            f"{_COLLECTIONS_PATH}/{collection}/{suffix}", json=payload, timeout=timeout
        )
        if resp.status_code >= 400 and _is_collection_missing(resp):
            name = self._evict_cached_id(collection)
            if name is not None:
                collection = await self.get_or_create_collection(name)
                resp = await self._client.post(
                    f"{_COLLECTIONS_PATH}/{collection}/{suffix}",
                    json=payload, timeout=timeout,
                )
        self._check(resp, action, collection)
        return resp

    async def upsert(
        self,
        collection: str,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
        documents: list[str],
    ) -> None:
        """批量写入/覆盖向量。metadata 值仅允许 str/int/float/bool。"""
        await self._post(
            "upsert",
            collection,
            "upsert",
            payload={
                "ids": ids,
                "embeddings": embeddings,
                "metadatas": metadatas,
                "documents": documents,
            },
            timeout=60,
        )

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
        resp = await self._post("query", collection, "query", payload=payload, timeout=30)
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
