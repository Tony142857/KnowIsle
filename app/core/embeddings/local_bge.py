"""本地 Embedding（默认）：bge-small-zh-v1.5，中文语义好、零成本、数据不出内网。

sentence-transformers + torch(CPU) 体积大，仅容器镜像安装（requirements-ml.txt），
因此一律惰性导入：import 本模块不要求该依赖存在，首次 embed 时才加载模型。
"""

import asyncio

from app.config import get_settings
from app.core.embeddings.base import BaseEmbedding


class LocalBgeEmbedding(BaseEmbedding):
    dim = 512  # bge-small-zh-v1.5

    _model = None  # 进程级单例，首次使用时加载

    @classmethod
    def _load(cls):
        if cls._model is None:
            from sentence_transformers import SentenceTransformer  # 惰性导入（镜像外无此依赖）

            name = get_settings().embedding_model
            if "/" not in name:
                name = f"BAAI/{name}"
            cls._model = SentenceTransformer(name)
        return cls._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        vectors = await asyncio.to_thread(
            model.encode, texts, normalize_embeddings=True
        )
        return [list(map(float, vec)) for vec in vectors]
