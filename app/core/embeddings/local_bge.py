"""本地 Embedding（默认）：bge-small-zh-v1.5，中文语义好、零成本、数据不出内网。

TODO(v0.3): 引入 sentence-transformers + torch(CPU)。体积较大，届时加入
requirements.txt 并在 worker 容器内完成模型下载/缓存，不污染宿主机。
"""

from app.core.embeddings.base import BaseEmbedding


class LocalBgeEmbedding(BaseEmbedding):
    pass
