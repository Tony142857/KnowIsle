"""黄金集离线检索评测；无数据库、网络或外部 LLM。

Docker：python scripts/evaluate_rag.py --grid --output tests/fixtures/rag_eval/report.json
默认使用确定性 Blake2b 特征哈希向量；报告中的数值全部由当次检索计算。
"""

import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from rank_bm25 import BM25Okapi

from app.core.chunking.semantic_splitter import split_blocks
from app.core.context import INDEX_VERSION, embedding_text
from app.core.parser.base import ParsedBlock
from app.core.retrieval.evidence import (
    filter_sufficient_evidence,
    needs_global_fallback,
    tokenize,
)
from app.core.retrieval.fusion import rrf_fuse

DIM = 256
DEFAULT_PARAMS = {
    "bm25_mode": "unigram_bigram",
    "rrf_k": 60,
    "semantic_weight": 0.65,
    "bm25_weight": 0.35,
    "candidate_k": 10,
    "top_k": 5,
    "mixed_personal_weight": 1.2,
    "global_fallback_max_distance": 2.0,
    "global_fallback_min_similarity": 0.05,
    "evidence_max_distance": 2.0,
    "evidence_min_similarity": 0.1,
    "evidence_min_lexical_coverage": 0.28,
    "evidence_weak_lexical_min_similarity": 0.3,
}


def _legacy_long(text: str, target_max: int = 600, overlap: int = 60) -> list[str]:
    import re
    sentences = [s for s in re.split(r"(?<=[。！？；\n])", text) if s.strip()]
    pieces, current = [], ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > target_max:
            pieces.append(current)
            current = current[-overlap:] + sentence
        else:
            current += sentence
    if current.strip():
        pieces.append(current)
    return pieces


def _legacy_chunks(document: dict) -> list[dict]:
    blocks = [ParsedBlock(**block) for block in document["blocks"]]
    segments, current, section = [], None, None
    for block in blocks:
        if block.level >= 1 and block.title:
            if current and current["content"].strip():
                segments.append(current)
            current = None
            section = None if block.level == 1 else block.title
        if not block.content.strip():
            continue
        if current is None:
            current = {"content": "", "section": section, "page_num": block.page_num}
        current["content"] += ("\n" if current["content"] else "") + block.content
    if current and current["content"].strip():
        segments.append(current)
    chunks = []
    for segment in segments:
        content = segment["content"]
        parts = [content] if len(content) <= 600 else _legacy_long(content)
        for part in parts:
            chunks.append({**segment, "content": part})
    merged = []
    for chunk in chunks:
        can_merge = merged and len(chunk["content"]) < 120
        can_merge = can_merge and len(merged[-1]["content"]) + len(chunk["content"]) <= 600
        if can_merge:
            merged[-1]["content"] += "\n" + chunk["content"]
        else:
            merged.append(chunk)
    return [{**chunk, "chunk_id": f"pub_d{document['id']}_{i:05d}",
             "chapter_id": document["chapter_id"]} for i, chunk in enumerate(merged, 1)]


def build_chunks(corpus: dict, optimized: bool, include_structure: bool = True) -> list[dict]:
    result = []
    for document in corpus["documents"]:
        if optimized:
            blocks = [ParsedBlock(**block) for block in document["blocks"]]
            chapter_ids = [document["chapter_id"] for _ in blocks]
            chunks = split_blocks(blocks, document["id"], document["scope"], chapter_ids)
        else:
            chunks = _legacy_chunks(document)
        for chunk in chunks:
            chunk.update(
                document_id=document["id"], course_id=document["course_id"],
                scope=document["scope"], owner_id=document["owner_id"],
                has_summary=document["has_summary"], file_type=document["file_type"],
            )
            chunk["search_text"] = (
                embedding_text(chunk) if optimized and include_structure else chunk["content"]
            )
            result.append(chunk)
    return result


def vector(text: str) -> list[float]:
    values = [0.0] * DIM
    features = tokenize(text, "unigram_bigram")
    for feature in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8, person=b"zhiyu-r1").digest()
        bucket = int.from_bytes(digest[:4], "little") % DIM
        sign = 1.0 if digest[4] & 1 else -1.0
        values[bucket] += sign
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _rank_semantic(query_vector, chunks, vectors, limit=10):
    ranked = sorted(
        chunks,
        key=lambda chunk: (-cosine(query_vector, vectors[chunk["chunk_id"]]), chunk["chunk_id"]),
    )
    return ranked[:limit]


def _rank_bm25(query: str, chunks, mode: str, limit=10):
    if not chunks:
        return []
    tokens = [tokenize(chunk["search_text"], mode) for chunk in chunks]
    query_tokens = set(tokenize(query, mode))
    model = BM25Okapi(tokens)
    scores = model.get_scores(tokenize(query, mode))
    significant = {token for token in query_tokens if len(token) > 1}
    overlap_tokens = significant or query_tokens
    if max(scores, default=0.0) <= 0:
        overlaps = [len(overlap_tokens.intersection(token_list)) for token_list in tokens]
        order = sorted(
            range(len(chunks)), key=lambda i: (-overlaps[i], chunks[i]["chunk_id"])
        )
        hits = [chunks[i] for i in order if overlaps[i] > 0]
    else:
        order = sorted(range(len(chunks)), key=lambda i: (-scores[i], chunks[i]["chunk_id"]))
        hits = [
            chunks[i] for i in order
            if overlap_tokens.intersection(tokens[i])
        ]
    return hits[:limit]


class _Hit:
    def __init__(self, chunk):
        self.chunk_id = chunk["chunk_id"]


def _fuse(first, second, top_k=5, weight_a=0.65, weight_b=0.35, k=60):
    fused = rrf_fuse([_Hit(c) for c in first], [_Hit(c) for c in second], k=k,
                     top_k=top_k, weight_a=weight_a, weight_b=weight_b)
    by_id = {chunk["chunk_id"]: chunk for chunk in [*first, *second]}
    return [by_id[chunk_id] for chunk_id, _ in fused]


def _scope_chunks(chunks, course_id, scope, owner_id):
    return [c for c in chunks if c["course_id"] == course_id and c["scope"] == scope
            and (scope != "personal" or c["owner_id"] == owner_id)]


def _evidence_hits(fused, semantic, bm25, query_vector, vectors):
    semantic_ids = {chunk["chunk_id"] for chunk in semantic}
    bm25_ids = {chunk["chunk_id"] for chunk in bm25}
    return [{
        "chunk": chunk,
        "sources": [name for name, present in (
            ("semantic", chunk["chunk_id"] in semantic_ids),
            ("bm25", chunk["chunk_id"] in bm25_ids),
        ) if present],
        "semantic_similarity": cosine(query_vector, vectors[chunk["chunk_id"]]),
    } for chunk in fused]


def _route(question, chunks, vectors, scope, optimized, params):
    started = time.perf_counter()
    owner_id = question.get("viewer_owner", 1)
    eligible = _scope_chunks(chunks, question["course_id"], scope, owner_id)
    coarse_started = time.perf_counter()
    groups = defaultdict(list)
    for chunk in eligible:
        if chunk["chapter_id"] is not None and chunk["has_summary"]:
            groups[chunk["chapter_id"]].append(chunk)
    chapter_vectors = {
        chapter_id: vector(" ".join(c["search_text"] for c in members))
        for chapter_id, members in groups.items()
    }
    if chapter_vectors and not params.get("global_only"):
        chapter_ids = sorted(
            chapter_vectors,
            key=lambda cid: (-cosine(vectors["query"], chapter_vectors[cid]), cid),
        )[:5]
    else:
        chapter_ids = None
    coarse_ms = (time.perf_counter() - coarse_started) * 1000
    fine_started = time.perf_counter()

    def search(chapter_filter):
        candidates = [c for c in eligible if chapter_filter is None or c["chapter_id"] in chapter_filter]
        semantic = _rank_semantic(
            vectors["query"], candidates, vectors, limit=params["candidate_k"]
        )
        bm25 = _rank_bm25(
            question["question"], candidates, params["bm25_mode"], limit=params["candidate_k"]
        )
        fused = _fuse(
            semantic, bm25, top_k=params["candidate_k"],
            weight_a=params["semantic_weight"], weight_b=params["bm25_weight"], k=params["rrf_k"],
        )
        return semantic, bm25, fused

    semantic, bm25, fused = search(chapter_ids)
    fallback = []
    evidence_metrics = {}
    if chapter_ids is None:
        fallback.append("no_coarse_summary")
    elif optimized:
        wrapped = _evidence_hits(fused, semantic, bm25, vectors["query"], vectors)
        fallback_reasons = needs_global_fallback(
            wrapped,
            chapter_ids=chapter_ids,
            min_hits=3,
            max_distance=params["global_fallback_max_distance"],
            min_similarity=params["global_fallback_min_similarity"],
        )
        if not fallback_reasons:
            outside = [c for c in eligible if c["chapter_id"] not in chapter_ids]
            outside_hits = _rank_bm25(
                question["question"], outside, params["bm25_mode"], limit=params["candidate_k"]
            )
            if any(c["chapter_id"] is None for c in outside_hits):
                fallback_reasons.append("unmapped_chunks")
            if any(not c["has_summary"] for c in outside_hits):
                fallback_reasons.append("no_summary_chapters")
        if fallback_reasons:
            fallback.extend(fallback_reasons)
            semantic, bm25, fused = search(None)
    if optimized:
        wrapped = _evidence_hits(fused, semantic, bm25, vectors["query"], vectors)
        accepted, evidence_metrics = filter_sufficient_evidence(
            question["question"],
            wrapped,
            max_distance=params["evidence_max_distance"],
            min_similarity=params["evidence_min_similarity"],
            min_lexical_coverage=params["evidence_min_lexical_coverage"],
            semantic_weak_lexical_min_similarity=params["evidence_weak_lexical_min_similarity"],
        )
        top = [hit["chunk"] for hit in accepted[:params["top_k"]]]
    else:
        top = fused[:params["top_k"]]
    fine_ms = (time.perf_counter() - fine_started) * 1000
    return {
        "top": top, "semantic": semantic, "bm25": bm25,
        "union": list({c["chunk_id"]: c for c in [*semantic, *bm25]}.values()),
        "chapters": chapter_ids, "fallback": fallback, "evidence": evidence_metrics,
        "coarse_ms": coarse_ms, "fine_ms": fine_ms,
        "total_ms": (time.perf_counter() - started) * 1000,
    }


def retrieve(question, chunks, optimized, params):
    vectors = {chunk["chunk_id"]: vector(chunk["search_text"]) for chunk in chunks}
    vectors["query"] = vector(question["question"])
    scopes = ["personal", "public"] if question["scope"] == "mixed" else [question["scope"]]
    routes = {scope: _route(question, chunks, vectors, scope, optimized, params) for scope in scopes}
    if len(scopes) == 1:
        result = routes[scopes[0]]
    else:
        personal, public = routes["personal"], routes["public"]
        top = _fuse(
            personal["top"], public["top"], top_k=params["top_k"],
            weight_a=params["mixed_personal_weight"], weight_b=1.0,
        )
        union = personal["union"] + public["union"]
        chapters = (personal["chapters"] or []) + (public["chapters"] or [])
        result = {
            "top": top,
            "semantic": personal["semantic"] + public["semantic"],
            "bm25": personal["bm25"] + public["bm25"],
            "union": list({c["chunk_id"]: c for c in union}.values()),
            "chapters": list(dict.fromkeys(chapters)) or None,
            "fallback": personal["fallback"] + public["fallback"],
            "evidence": {"personal": personal["evidence"], "public": public["evidence"]},
            "coarse_ms": personal["coarse_ms"] + public["coarse_ms"],
            "fine_ms": personal["fine_ms"] + public["fine_ms"],
            "total_ms": personal["total_ms"] + public["total_ms"],
            "routes": routes,
        }
    return result


def _percentile(values, percentile):
    if not values:
        return 0.0
    values = sorted(values)
    position = (len(values) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _quality(rows, key):
    answerable = [row for row in rows if row["question"]["answerable"]]
    if not answerable:
        return {"count": 0}
    def mean(metric):
        return statistics.fmean(row[key][metric] for row in answerable)
    return {
        "count": len(answerable), "chunk_recall@5": mean("recall"),
        "hit@1": mean("hit1"), "mrr": mean("mrr"), "ndcg@5": mean("ndcg"),
        "chapter_hit@5": mean("chapter_hit"), "chapter_recall@5": mean("chapter_recall"),
        "semantic_recall@10": mean("semantic_recall"), "bm25_recall@10": mean("bm25_recall"),
        "union_recall@10": mean("union_recall"),
    }


def evaluate(questions, corpus, chunks, optimized, params, precomputed=None):
    params = {**DEFAULT_PARAMS, **params}
    documents = {doc["id"]: doc for doc in corpus["documents"]}
    chunks_by_id = {chunk["chunk_id"]: chunk for chunk in chunks}
    rows = []
    for question in questions:
        result = (
            precomputed[question["qid"]] if precomputed is not None
            else retrieve(question, chunks, optimized, params)
        )
        gold = {c["chunk_id"] for c in chunks for anchor in question["anchors"]
                if c["document_id"] == anchor["document_id"] and anchor["text"] in c["content"]}
        top_ids = [c["chunk_id"] for c in result["top"]]
        relevant = [1 if chunk_id in gold else 0 for chunk_id in top_ids]
        first = next((rank for rank, value in enumerate(relevant, 1) if value), None)
        dcg = sum(value / math.log2(rank + 1) for rank, value in enumerate(relevant, 1))
        ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(len(gold), 5) + 1))
        gold_chapters = {documents[a["document_id"]]["chapter_id"] for a in question["anchors"]
                         if documents[a["document_id"]]["chapter_id"] is not None}
        coarse = set(result["chapters"] or [])

        def route_recall(name, result=result, gold=gold):
            ids = {c["chunk_id"] for c in result[name]}
            return len(gold & ids) / len(gold) if gold else 0.0
        owner = question.get("viewer_owner", 1)
        allowed = set()
        for scope in (["personal", "public"] if question["scope"] == "mixed" else [question["scope"]]):
            allowed.update(c["chunk_id"] for c in _scope_chunks(chunks, question["course_id"], scope, owner))
        candidate_ids = {chunk["chunk_id"] for chunk in result["union"]}
        leaks = sorted(candidate_ids.difference(allowed))
        coarse_missed = False
        for chunk_id in gold:
            chunk = chunks_by_id[chunk_id]
            route = result.get("routes", {}).get(chunk["scope"], result)
            if route["chapters"] is not None and chunk["chapter_id"] not in route["chapters"]:
                coarse_missed = True
                break
        rows.append({
            "question": question, "gold_chunks": sorted(gold), "returned": top_ids,
            "coarse_missed": coarse_missed,
            "gold_chapters": sorted(gold_chapters), "coarse_chapters": sorted(coarse),
            "union": [chunk["chunk_id"] for chunk in result["union"]],
            "fallback": result["fallback"], "permission_leaks": leaks,
            "evidence": result["evidence"],
            "private_adversarial": question["scope"] in {"personal", "mixed"} and owner != 1,
            "metrics": {
                "recall": len(gold & set(top_ids)) / len(gold) if gold else 0.0,
                "hit1": float(bool(relevant and relevant[0])), "mrr": 1 / first if first else 0.0,
                "ndcg": dcg / ideal if ideal else 0.0,
                "chapter_hit": float(bool(gold_chapters & coarse)) if gold_chapters else 0.0,
                "chapter_recall": len(gold_chapters & coarse) / len(gold_chapters) if gold_chapters else 0.0,
                "semantic_recall": route_recall("semantic"), "bm25_recall": route_recall("bm25"),
                "union_recall": route_recall("union"),
            },
            "timing": {key: result[key] for key in ("coarse_ms", "fine_ms", "total_ms")},
            "unanswerable_error": not question["answerable"] and bool(top_ids),
        })
    unanswered = [row for row in rows if not row["question"]["answerable"]]
    by_scope = {scope: _quality([r for r in rows if r["question"]["scope"] == scope], "metrics")
                for scope in ("personal", "public", "mixed")}
    correct_rejection = statistics.fmean(
        not row["unanswerable_error"] for row in unanswered
    ) if unanswered else 0.0
    context_error = statistics.fmean(
        row["unanswerable_error"] for row in unanswered
    ) if unanswered else 0.0
    def bad_stage(row):
        if row["permission_leaks"]:
            return "citation"
        if row["unanswerable_error"]:
            return "generation"
        if not row["gold_chunks"]:
            return "chunking"
        if row["coarse_missed"]:
            return "coarse"
        return "fine"

    bad_cases = [
        {
            "qid": row["question"]["qid"], "category": row["question"]["category"],
            "stage": bad_stage(row),
            "reason": (
                "permission_leak" if row["permission_leaks"]
                else "unanswerable_context" if row["unanswerable_error"]
                else "retrieval_miss"
            ),
            "gold_chunks": row["gold_chunks"], "returned": row["returned"],
            "fallback": row["fallback"],
        }
        for row in rows
        if row["permission_leaks"] or row["unanswerable_error"]
        or (row["question"]["answerable"] and row["metrics"]["recall"] < 1)
    ]
    coarse_misses = [row for row in rows if row["coarse_missed"]]
    fallback_recovery = statistics.fmean(
        row["metrics"]["recall"] == 1 for row in coarse_misses
    ) if coarse_misses else 0.0
    return {
        "overall": _quality(rows, "metrics"),
        "tune": _quality([r for r in rows if r["question"]["split"] == "tune"], "metrics"),
        "frozen": _quality([r for r in rows if r["question"]["split"] == "frozen"], "metrics"),
        "by_scope": by_scope,
        "unanswerable": {
            "count": len(unanswered),
            "correct_rejection_rate": correct_rejection,
            "context_error_rate": context_error,
        },
        "fallback_rate": statistics.fmean(bool(r["fallback"]) for r in rows) if rows else 0.0,
        "coarse_gold_miss_rate": statistics.fmean(row["coarse_missed"] for row in rows) if rows else 0.0,
        "coarse_fallback_full_recovery_rate": fallback_recovery,
        "permission_leaks": sum(len(r["permission_leaks"]) for r in rows),
        "private_adversarial": {
            "count": sum(row["private_adversarial"] for row in rows),
            "permission_leaks": sum(
                len(row["permission_leaks"]) for row in rows if row["private_adversarial"]
            ),
        },
        "bad_case_taxonomy": ["chunking", "coarse", "fine", "citation", "generation"],
        "performance_ms": {stage: {
            "p50": round(_percentile([r["timing"][stage] for r in rows], 0.5), 3),
            "p95": round(_percentile([r["timing"][stage] for r in rows], 0.95), 3),
        } for stage in ("coarse_ms", "fine_ms", "total_ms")},
        "bad_cases": bad_cases,
    }


def _dataset_hash(fixture_dir):
    digest = hashlib.sha256()
    for name in ("corpus.json", "questions.jsonl"):
        digest.update((fixture_dir / name).read_bytes())
    return digest.hexdigest()


def _commit():
    if os.environ.get("RAG_EVAL_COMMIT"):
        return os.environ["RAG_EVAL_COMMIT"]
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return "unavailable-in-container"
    return result.stdout.strip() or "unavailable"


async def evaluate_container(questions, corpus, params):
    """只读真实 PG/Redis/Chroma/BGE；fixture 未对齐时拒绝产生伪生产指标。"""
    from types import SimpleNamespace

    from fastapi import HTTPException
    from sqlalchemy import select

    from app.config import get_settings
    from app.core.pipeline import retrieve as production_retrieve
    from app.storage.db import SessionLocal
    from app.storage.models import Chapter, Chunk, Course, Document, User

    async with SessionLocal() as session:
        courses = list((await session.execute(select(Course))).scalars().all())
        documents = list((await session.execute(select(Document))).scalars().all())
        db_chunks = list((await session.execute(select(Chunk))).scalars().all())
        chapters = list((await session.execute(select(Chapter))).scalars().all())
        user_ids = list((await session.execute(select(User.id))).scalars().all())
        course_by_name = {course.name: course for course in courses}
        missing_courses = [c["name"] for c in corpus["courses"] if c["name"] not in course_by_name]
        if missing_courses:
            raise RuntimeError(f"真实库缺少 fixture 课程：{missing_courses}；请在隔离对齐库运行")
        course_map = {c["id"]: course_by_name[c["name"]].id for c in corpus["courses"]}
        doc_map = {}
        for fixture in corpus["documents"]:
            matches = [doc for doc in documents if (
                doc.file_name == fixture["file_name"]
                and doc.course_id == course_map[fixture["course_id"]]
                and doc.scope == fixture["scope"]
            )]
            if len(matches) != 1:
                raise RuntimeError(
                    f"fixture 文档 {fixture['file_name']} 在真实库匹配 {len(matches)} 条；"
                    "请在隔离对齐库运行，脚本不会自动灌数或重建"
                )
            doc_map[fixture["id"]] = matches[0]
        chapter_by_id = {chapter.id: chapter for chapter in chapters}
        course_ids = set(course_map.values())
        db_chunks = [chunk for chunk in db_chunks if chunk.course_id in course_ids]

        def chunk_row(chunk):
            chapter = chapter_by_id.get(chunk.chapter_id)
            return {
                "chunk_id": chunk.chunk_id, "document_id": chunk.document_id,
                "course_id": chunk.course_id, "chapter_id": chunk.chapter_id,
                "owner_id": chunk.owner_id, "scope": chunk.scope,
                "section": chunk.section, "page_num": chunk.page_num,
                "file_type": chunk.file_type, "content": chunk.content,
                "search_text": chunk.content,
                "has_summary": bool(chapter and chapter.summary_vector_id),
            }

        chunks = [chunk_row(chunk) for chunk in db_chunks]
        chunk_by_id = {chunk["chunk_id"]: chunk for chunk in chunks}
        aligned_docs = []
        for fixture in corpus["documents"]:
            actual = doc_map[fixture["id"]]
            own_chunks = [chunk for chunk in db_chunks if chunk.document_id == actual.id]
            first_chapter = own_chunks[0].chapter_id if own_chunks else None
            chapter = chapter_by_id.get(first_chapter)
            aligned_docs.append({
                **fixture, "id": actual.id, "course_id": actual.course_id,
                "owner_id": actual.owner_id, "chapter_id": first_chapter,
                "has_summary": bool(chapter and chapter.summary_vector_id),
            })
        aligned_corpus = {**corpus, "documents": aligned_docs}
        personal_owners = [c.owner_id for c in courses if c.id in course_ids and c.owner_id is not None]
        owner_one = personal_owners[0] if personal_owners else aligned_docs[0]["owner_id"]
        owner_other = next((user_id for user_id in user_ids if user_id != owner_one), -1)
        aligned_questions = []
        for question in questions:
            aligned = json.loads(json.dumps(question))
            aligned["course_id"] = course_map[question["course_id"]]
            aligned["viewer_owner"] = owner_one if question.get("viewer_owner", 1) == 1 else owner_other
            for anchor in aligned["anchors"]:
                anchor["document_id"] = doc_map[anchor["document_id"]].id
            aligned_questions.append(aligned)

        precomputed = {}
        observed = {"index_versions": set(), "embedding_models": set(), "states": set()}
        for question in aligned_questions:
            metrics = {}
            try:
                hits, chapter_ids = await production_retrieve(
                    session, question["question"], SimpleNamespace(id=question["viewer_owner"]),
                    question["course_id"], question["scope"],
                )
                metrics = getattr(hits, "metrics", {})
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                hits, chapter_ids = [], None
                metrics = {"fallback": ["course_access_denied"], "degraded": []}
            scoped_metrics = [metrics, *metrics.get("scopes", {}).values()]
            for item in scoped_metrics:
                if item.get("index_version"):
                    observed["index_versions"].add(item["index_version"])
                if item.get("embedding_model"):
                    observed["embedding_models"].add(item["embedding_model"])
                if item.get("index_state"):
                    observed["states"].add(item["index_state"])
            top = [chunk_by_id[hit["chunk"].chunk_id] for hit in hits]
            semantic = [
                chunk_by_id[hit["chunk"].chunk_id] for hit in hits
                if "semantic" in hit.get("sources", [])
            ]
            bm25 = [
                chunk_by_id[hit["chunk"].chunk_id] for hit in hits
                if "bm25" in hit.get("sources", [])
            ]

            def elapsed(name, metrics=metrics):
                return metrics.get(name, sum(
                    item.get(name, 0.0) for item in metrics.get("scopes", {}).values()
                ))

            precomputed[question["qid"]] = {
                "top": top, "semantic": semantic, "bm25": bm25, "union": top,
                "chapters": chapter_ids, "fallback": metrics.get("fallback", []),
                "evidence": {"weak_evidence": metrics.get("weak_evidence", False)},
                "coarse_ms": elapsed("coarse_ms"), "fine_ms": elapsed("fine_ms"),
                "embedding_ms": metrics.get("embedding_ms", 0.0),
                "total_ms": metrics.get("retrieval_ms", 0.0),
            }
    report = evaluate(
        aligned_questions, aligned_corpus, chunks, True, params, precomputed=precomputed
    )
    metadata = {
        "mode": "container",
        "performance_kind": "production PG/Redis/Chroma/BGE; external LLM disabled",
        "model": {"name": get_settings().embedding_model, "external_llm": False},
        "index_versions": sorted(observed["index_versions"]),
        "embedding_models": sorted(observed["embedding_models"]),
        "collection_states": sorted(observed["states"]),
        "note": "只读真实依赖；候选路 Recall 仅基于当次最终可见 hits，不冒充原始候选全集。",
    }
    return report, metadata


def _source_fingerprint():
    names = [
        "app/core/chunking/semantic_splitter.py",
        "app/core/context.py",
        "app/core/indexing.py",
        "app/core/pipeline.py",
        "app/core/retrieval/coarse.py",
        "app/core/retrieval/evidence.py",
        "app/core/retrieval/fine.py",
        "app/core/retrieval/fusion.py",
        "scripts/evaluate_rag.py",
        "scripts/rebuild_rag_index.py",
    ]
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode())
        digest.update(Path(name).read_bytes())
    dirty = None
    if os.environ.get("RAG_EVAL_SOURCE_DIRTY") is not None:
        dirty = os.environ["RAG_EVAL_SOURCE_DIRTY"] == "1"
    else:
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "--", *names],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0:
                dirty = bool(result.stdout.strip())
        except FileNotFoundError:
            pass
    return {"sha256": digest.hexdigest(), "dirty": dirty, "files": len(names)}


def _select_evidence_params(tune, corpus, chunks, params):
    """只在 tune 选择阈值：先满足不可回答零上下文物，再优化召回与排序。"""
    candidates = []
    for similarity in (0.05, 0.1, 0.15, 0.2, 0.25, 0.3):
        for coverage in (0.2, 0.28, 0.35, 0.45):
            for weak_lexical in (0.3, 0.35, 0.4):
                candidate = {
                    **params,
                    "evidence_min_similarity": similarity,
                    "evidence_weak_lexical_min_similarity": weak_lexical,
                    "global_fallback_min_similarity": min(similarity, 0.1),
                    "evidence_min_lexical_coverage": coverage,
                }
                report = evaluate(tune, corpus, chunks, True, candidate)
                quality = report["overall"]
                rejection = report["unanswerable"]["correct_rejection_rate"]
                candidates.append({
                    "params": candidate,
                    "quality": quality,
                    "rejection": rejection,
                    "row": {
                        "evidence_min_similarity": similarity,
                        "evidence_min_lexical_coverage": coverage,
                        "evidence_weak_lexical_min_similarity": weak_lexical,
                        "correct_rejection_rate": rejection,
                        "chunk_recall@5": quality.get("chunk_recall@5", 0.0),
                        "ndcg@5": quality.get("ndcg@5", 0.0),
                    },
                })
    selected = max(
        candidates,
        key=lambda item: (
            item["rejection"] == 1.0,
            item["rejection"],
            item["quality"].get("chunk_recall@5", 0.0),
            item["quality"].get("ndcg@5", 0.0),
            -item["params"]["evidence_min_similarity"],
            -item["params"]["evidence_weak_lexical_min_similarity"],
        ),
    )
    return {**DEFAULT_PARAMS, **selected["params"]}, [item["row"] for item in candidates]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", type=Path, default=Path("tests/fixtures/rag_eval"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--grid", action="store_true", help="仅使用 tune 做 RRF 网格对照")
    args = parser.parse_args()
    corpus = json.loads((args.fixture_dir / "corpus.json").read_text(encoding="utf-8"))
    question_lines = (args.fixture_dir / "questions.jsonl").read_text(encoding="utf-8").splitlines()
    questions = [json.loads(line) for line in question_lines if line.strip()]
    params = {
        "bm25_mode": "unigram_bigram", "rrf_k": 60, "semantic_weight": 0.65,
        "bm25_weight": 0.35, "candidate_k": 10, "top_k": 5, "mixed_personal_weight": 1.2,
    }
    baseline_params = {**params, "bm25_mode": "bigram"}
    optimized_chunks = build_chunks(corpus, True)
    baseline = evaluate(questions, corpus, build_chunks(corpus, False), False, baseline_params)
    tune_questions = [q for q in questions if q["split"] == "tune"]
    selected_params, evidence_grid = _select_evidence_params(
        tune_questions, corpus, optimized_chunks, params
    )
    optimized = evaluate(questions, corpus, optimized_chunks, True, selected_params)
    token_modes = {}
    for mode in ("unigram", "bigram", "unigram_bigram"):
        mode_params = {**selected_params, "bm25_mode": mode}
        token_modes[mode] = evaluate(
            questions, corpus, optimized_chunks, True, mode_params
        )
    grid = []
    if args.grid:
        for k in (20, 60, 100):
            for semantic_weight, bm25_weight in ((0.65, 0.35), (0.5, 0.5), (0.8, 0.2)):
                candidate = {
                    **selected_params, "rrf_k": k,
                    "semantic_weight": semantic_weight, "bm25_weight": bm25_weight,
                }
                report = evaluate(tune_questions, corpus, optimized_chunks, True, candidate)
                grid.append({"rrf_k": k, "semantic_weight": semantic_weight, "bm25_weight": bm25_weight,
                             "tune": report["overall"]})
    gate = {
        "tune_recall_improved": (
            optimized["tune"]["chunk_recall@5"] > baseline["tune"]["chunk_recall@5"]
        ),
        "tune_ndcg_improved": (
            optimized["tune"]["ndcg@5"] > baseline["tune"]["ndcg@5"]
        ),
        "frozen_recall_no_regression": (
            optimized["frozen"]["chunk_recall@5"] >= baseline["frozen"]["chunk_recall@5"]
        ),
        "frozen_ndcg_no_regression": (
            optimized["frozen"]["ndcg@5"] >= baseline["frozen"]["ndcg@5"]
        ),
        "permission_leaks_zero": optimized["permission_leaks"] == 0,
        "unanswerable_all_rejected": (
            optimized["unanswerable"]["correct_rejection_rate"] == 1.0
        ),
        "dataset_at_least_60_questions": len(questions) >= 60,
        "dataset_three_courses": len(corpus["courses"]) >= 3,
    }
    gate["passed"] = all(gate.values())
    report = {
        "schema": 1, "generated_at": datetime.now(UTC).isoformat(),
        "commit": _commit(),
        "dataset": {"sha256": _dataset_hash(args.fixture_dir), "questions": len(questions),
                    "courses": len(corpus["courses"]), "documents": len(corpus["documents"]),
                    "formats": sorted({d["file_type"] for d in corpus["documents"]}),
                    "tune": sum(q["split"] == "tune" for q in questions),
                    "frozen": sum(q["split"] == "frozen" for q in questions)},
        "model": {"name": "deterministic-blake2b-feature-hash", "dimension": DIM, "external_llm": False},
        "index_version": INDEX_VERSION, "parameters": selected_params,
        "parameters_selected_on": "tune-only",
        "source": _source_fingerprint(),
        "baseline": baseline, "optimized": optimized, "bm25_tokenizer_comparison": token_modes,
        "evidence_threshold_grid": evidence_grid,
        "gate": gate,
        "tune_only_rrf_grid": grid,
        "citation_and_generation_review": (
            "not-run: external LLM disabled; requires fixed-context human review"
        ),
        "note": "网格与阈值只评估 tune；未根据 frozen 选参。数值是本次离线重放结果，不代表外部模型生成质量。",
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    summary = {
        "dataset": report["dataset"], "baseline": baseline["overall"],
        "optimized": optimized["overall"], "permission_leaks": optimized["permission_leaks"],
        "unanswerable": optimized["unanswerable"],
        "output": str(args.output) if args.output else None,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
