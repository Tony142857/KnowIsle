"""检索结果融合排序（模块 A2）：双路 RRF 融合。"""


def rrf_fuse(list_a, list_b, k: int = 60, top_k: int = 5,
             weight_a: float = 0.65, weight_b: float = 0.35):
    """倒数秩融合：score = Σ weight/(k + rank)，对两路结果排序融合（文档 §A2）。

    §20.5 B2（v1.1）：两路及权重均为显式参数（消除借参 hack）——
    fine_search 传（语义 0.65 / BM25 0.35）；pipeline mixed 分支传
    （个人库 1.2 / 公共库 1.0，个人库加权见 §5.3）。默认 0.65 / 0.35 行为不变。
    hits 需为带 chunk_id 属性的对象列表，返回 [(chunk_id, score), ...]。
    """
    scores: dict[str, float] = {}
    for rank, hit in enumerate(list_a):
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0) + weight_a / (k + rank + 1)
    for rank, hit in enumerate(list_b):
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0) + weight_b / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])[:top_k]
