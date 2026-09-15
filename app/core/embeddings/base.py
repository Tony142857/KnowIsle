"""Embedding 统一接口（§10.3）：本地 bge 与官方接口可切换。"""


class BaseEmbedding:
    dim: int = 512  # bge-small-zh-v1.5

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError
