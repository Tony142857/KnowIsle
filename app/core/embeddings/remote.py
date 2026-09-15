"""官方接口 Embedding（可切换）：OpenAI 兼容 /embeddings 端点。"""

from app.core.embeddings.base import BaseEmbedding


class RemoteEmbedding(BaseEmbedding):
    # TODO(v0.3): 走统一 Embedding 接口层，EMBEDDING_MODE=remote 时启用
    pass
