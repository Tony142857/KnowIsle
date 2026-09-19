"""Embedding 统一接口（§10.3）：本地 bge 与官方接口可切换。"""

from app.config import get_settings
from app.core.embeddings.base import BaseEmbedding


def get_embedding() -> BaseEmbedding:
    """按 EMBEDDING_MODE 分发：local（默认，bge 本地模型）/ remote（OpenAI 兼容接口）。"""
    if get_settings().embedding_mode == "remote":
        from app.core.embeddings.remote import RemoteEmbedding

        return RemoteEmbedding()
    from app.core.embeddings.local_bge import LocalBgeEmbedding

    return LocalBgeEmbedding()
