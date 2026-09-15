"""检索结果融合排序（模块 A2）：语义 + BM25 双路 RRF 融合。"""


def rrf_fuse(semantic_hits, bm25_hits, k: int = 60, top_k: int = 5,
             weight_semantic: float = 0.65, weight_bm25: float = 0.35):
    """倒数秩融合：score = Σ weight/(k + rank)，对两路结果排序融合（文档 §A2）。

    语义检索权重 60%-70%，BM25 权重 30%-40%，默认 0.65 / 0.35。
    hits 需为带 chunk_id 属性的对象列表，返回 [(chunk_id, score), ...]。
    """
    scores: dict[str, float] = {}
    for rank, hit in enumerate(semantic_hits):
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0) + weight_semantic / (k + rank + 1)
    for rank, hit in enumerate(bm25_hits):
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0) + weight_bm25 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])[:top_k]
