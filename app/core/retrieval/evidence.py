"""生产检索与离线评测共用的弱证据、全局补充和词法策略。"""

from typing import Any


def tokenize(text: str, mode: str = "unigram_bigram") -> list[str]:
    if mode not in {"unigram", "bigram", "unigram_bigram"}:
        raise ValueError("未知 BM25 分词模式")
    tokens: list[str] = []
    word: list[str] = []
    prev_cjk: str | None = None

    def flush_word() -> None:
        if word:
            tokens.append("".join(word))
            word.clear()

    for ch in text:
        if "一" <= ch <= "鿿":
            flush_word()
            if mode in {"unigram", "unigram_bigram"}:
                tokens.append(ch)
            if mode in {"bigram", "unigram_bigram"} and prev_cjk is not None:
                tokens.append(prev_cjk + ch)
            prev_cjk = ch
        elif ch.isalnum() or ch == "_":
            word.append(ch.lower())
            prev_cjk = None
        else:
            flush_word()
            prev_cjk = None
    flush_word()
    return tokens


def has_lexical_overlap(query_tokens: set[str], document_tokens: list[str]) -> bool:
    significant = {token for token in query_tokens if len(token) > 1}
    return bool((significant or query_tokens).intersection(document_tokens))


def lexical_overlap_count(query_tokens: set[str], document_tokens: list[str]) -> int:
    """命中查询显著词的个数；BM25 IDF 全为负时作为稳定降级排序。"""
    significant = {token for token in query_tokens if len(token) > 1}
    return len((significant or query_tokens).intersection(document_tokens))


def lexical_coverage(query: str, text: str, mode: str = "unigram_bigram") -> float:
    query_tokens = {token for token in tokenize(query, mode) if len(token) > 1}
    if not query_tokens:
        query_tokens = set(tokenize(query, mode))
    if not query_tokens:
        return 0.0
    return len(query_tokens.intersection(tokenize(text, mode))) / len(query_tokens)


def _value(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _chunk(hit: dict) -> Any:
    return hit["chunk"]


def semantic_similarity(hit: dict) -> float | None:
    direct = hit.get("semantic_similarity")
    if direct is not None:
        return float(direct)
    distance = hit.get("distance", getattr(_chunk(hit), "_rag_distance", None))
    return None if distance is None else 1.0 - float(distance) / 2.0


def semantic_evidence(
    hit: dict,
    *,
    max_distance: float,
    min_similarity: float | None = None,
) -> bool:
    if "semantic" not in hit.get("sources", []):
        return False
    distance = hit.get("distance", getattr(_chunk(hit), "_rag_distance", None))
    similarity = semantic_similarity(hit)
    if min_similarity is not None and similarity is not None:
        return similarity >= min_similarity
    if distance is None:
        return similarity is None or similarity >= 1.0 - max_distance / 2.0
    return float(distance) <= max_distance


def filter_sufficient_evidence(
    query: str,
    hits: list[dict],
    *,
    max_distance: float,
    min_similarity: float | None = None,
    min_lexical_coverage: float,
    semantic_weak_lexical_min_similarity: float | None = None,
    degraded: list[str] | None = None,
) -> tuple[list[dict], dict]:
    """过滤无关 TopK；任一路故障时仍允许另一路提供可解释证据。

    semantic_weak_lexical_min_similarity：词面覆盖不足（低于 min_lexical_coverage）
    的语义候选需要更高相似度才可作为证据，避免“问下载链接”这类问题只靠泛主题
    相似度蹭到无关上下文；相似度未知时不做额外收紧。
    """
    degraded = degraded or []
    semantic_available = not any(
        reason == "index_version_mismatch" or reason.startswith("semantic")
        for reason in degraded
    )
    bm25_available = "bm25" not in degraded
    semantic_present = any("semantic" in hit.get("sources", []) for hit in hits)
    semantic_anchor = any(
        semantic_evidence(hit, max_distance=max_distance, min_similarity=min_similarity)
        for hit in hits
    )
    accepted: list[dict] = []
    rejected: list[dict] = []
    for hit in hits:
        sources = hit.get("sources", [])
        chunk = _chunk(hit)
        text = _value(chunk, "search_text") or _value(chunk, "content") or ""
        coverage = lexical_coverage(query, text)
        hit["lexical_coverage"] = round(coverage, 6)
        semantic_ok = semantic_available and semantic_evidence(
            hit, max_distance=max_distance, min_similarity=min_similarity
        )
        if (
            semantic_ok
            and semantic_weak_lexical_min_similarity is not None
            and coverage < min_lexical_coverage
        ):
            similarity = semantic_similarity(hit)
            if similarity is not None and similarity < semantic_weak_lexical_min_similarity:
                semantic_ok = False
        lexical_ok = bm25_available and "bm25" in sources and coverage >= min_lexical_coverage
        # 语义路有候选但无足够证据时，词面命中需有语义锚点兜底，避免无关 BM25
        # TopK 被当成证据；语义路整体为空/降级时仍允许 BM25 单路供给（R3）。
        lexical_ok = lexical_ok and (
            not semantic_available or not semantic_present or semantic_anchor
        )
        if semantic_ok or lexical_ok:
            accepted.append(hit)
        else:
            rejected.append(hit)
    metrics = {
        "weak_evidence": bool(hits) and not accepted,
        "evidence_rejected": len(rejected),
        "evidence_threshold": {
            "max_distance": max_distance,
            "min_similarity": min_similarity,
            "min_lexical_coverage": min_lexical_coverage,
            "semantic_weak_lexical_min_similarity": semantic_weak_lexical_min_similarity,
        },
        "best_semantic_similarity": max(
            (similarity for hit in hits if (similarity := semantic_similarity(hit)) is not None),
            default=None,
        ),
    }
    return accepted, metrics


def needs_global_fallback(
    hits: list[dict],
    *,
    chapter_ids: list[int] | None,
    min_hits: int,
    max_distance: float,
    min_similarity: float | None = None,
) -> list[str]:
    if not chapter_ids:
        return []
    if len(hits) < min_hits:
        return ["insufficient_chapter_hits"]
    if not any(
        semantic_evidence(hit, max_distance=max_distance, min_similarity=min_similarity)
        for hit in hits
    ):
        return ["weak_chapter_evidence"]
    return []
