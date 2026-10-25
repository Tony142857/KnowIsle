"""v1.3 RAG 机制与黄金集回归；全部使用内存数据，不访问外部服务。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import review
from app.community.posts import build_ai_citations
from app.core.chunking.semantic_splitter import OVERLAP, TARGET_MAX, split_blocks
from app.core.parser.base import ParsedBlock
from app.core.retrieval.fine import tokenize
from scripts import evaluate_rag, rebuild_rag_index

from .fakestack import FakeResult, FakeSession, make_chunk, make_doc, make_post

FIXTURES = Path("tests/fixtures/rag_eval")


def test_hard_split_without_punctuation_respects_limit_overlap_and_ids():
    blocks = [ParsedBlock(title="长文", content="甲" * 1300, level=1)]
    # 一级标题自身也作为原文块输入时，标题内容仍保持原样，不注入路径。
    chunks = split_blocks(blocks, 8, "personal")
    assert chunks and all(len(chunk["content"]) <= TARGET_MAX for chunk in chunks)
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current["content"][:OVERLAP] == previous["content"][-OVERLAP:]
    assert [chunk["chunk_id"] for chunk in chunks] == [f"per_d8_{i:05d}" for i in range(1, len(chunks) + 1)]


def test_target_min_merges_consecutive_short_paragraphs_and_keeps_raw_content():
    blocks = [
        ParsedBlock(title="分页", content="", level=1),
        ParsedBlock(title="地址转换", content="", level=2),
        ParsedBlock(title=None, content="甲" * 100, level=0),
        ParsedBlock(title=None, content="乙" * 100, level=0),
        ParsedBlock(title=None, content="丙" * 100, level=0),
    ]
    chunks = split_blocks(blocks, 9, "public")
    assert len(chunks) == 1 and len(chunks[0]["content"]) == 302
    assert chunks[0]["content"] == "甲" * 100 + "\n" + "乙" * 100 + "\n" + "丙" * 100
    assert chunks[0]["embedding_text"].startswith("分页 > 地址转换 > 正文\n")
    assert chunks[0]["prompt_text"].startswith("[pub_d9_00001]\n分页 > 地址转换 > 正文")
    assert "分页 >" not in chunks[0]["content"]


def test_bm25_tokenizer_three_modes_are_distinct():
    assert tokenize("分页存储", "unigram") == ["分", "页", "存", "储"]
    assert tokenize("分页存储", "bigram") == ["分页", "页存", "存储"]
    assert tokenize("分页存储", "unigram_bigram") == [
        "分", "页", "分页", "存", "页存", "储", "存储"
    ]


def _questions():
    lines = (FIXTURES / "questions.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_golden_dataset_coverage_splits_and_stable_anchors():
    corpus = json.loads((FIXTURES / "corpus.json").read_text(encoding="utf-8"))
    questions = _questions()
    documents = {doc["id"]: doc for doc in corpus["documents"]}
    assert len(questions) >= 60 and len({q["qid"] for q in questions}) == len(questions)
    assert len(corpus["courses"]) >= 3
    assert {doc["file_type"] for doc in corpus["documents"]} == {
        "pdf_textbook", "word", "ppt", "markdown"
    }
    assert {q["scope"] for q in questions} == {"personal", "public", "mixed"}
    assert {q["split"] for q in questions} == {"tune", "frozen"}
    assert {q["split"] for q in questions if q["scope"] == "mixed"} == {"tune", "frozen"}
    categories = {q["category"] for q in questions}
    assert {
        "concept", "detail", "cross_paragraph", "cross_chapter",
        "english_number", "colloquial", "unanswerable",
    } <= categories
    for question in questions:
        assert question["answerable"] == bool(question["anchors"])
        for anchor in question["anchors"]:
            original = "\n".join(block["content"] for block in documents[anchor["document_id"]]["blocks"])
            assert anchor["text"] in original, (question["qid"], anchor)


def test_offline_evaluator_is_deterministic_and_checks_permissions():
    corpus = json.loads((FIXTURES / "corpus.json").read_text(encoding="utf-8"))
    questions = _questions()[:2]
    chunks = evaluate_rag.build_chunks(corpus, True)
    params = {
        "bm25_mode": "unigram_bigram", "rrf_k": 60,
        "semantic_weight": 0.65, "bm25_weight": 0.35,
    }
    first = evaluate_rag.evaluate(questions, corpus, chunks, True, params)
    second = evaluate_rag.evaluate(questions, corpus, chunks, True, params)
    assert first["overall"] == second["overall"]
    assert first["permission_leaks"] == 0
    assert 0 <= first["overall"]["chunk_recall@5"] <= 1
    assert {"coarse_ms", "fine_ms", "total_ms"} <= set(first["performance_ms"])


@pytest.mark.parametrize("mutation", ["count", "type", "options", "answer", "evidence"])
def test_quiz_rejects_invalid_structure(mutation):
    item = {
        "type": "single_choice", "question": "问题", "options": ["A. 1", "B. 2", "C. 3", "D. 4"],
        "answer": "A", "chunk_id": "pub_d1_00001",
    }
    items = [item]
    if mutation == "count":
        expected = 2
    else:
        expected = 1
        if mutation == "type":
            item["type"] = "unknown"
        elif mutation == "options":
            item["options"] = item["options"][:2]
        elif mutation == "answer":
            item["answer"] = ""
        elif mutation == "evidence":
            item["chunk_id"] = "pub_d9_99999"
    raw = json.dumps({"questions": items})
    assert review._parse_quiz(raw, {"pub_d1_00001"}, expected) is None


def test_quiz_returns_document_id_and_outline_budget():
    item = {"type": "short_answer", "question": "问题", "answer": "答案", "chunk_id": "pub_d1_00001"}
    parsed = review._parse_quiz(
        json.dumps({"questions": [item]}), {"pub_d1_00001"}, 1, {"pub_d1_00001": 11}
    )
    assert parsed[0]["document_id"] == 11
    sections = [{"id": 1, "title": "本章", "chunks": [
        {"chunk": make_chunk("pub_d1_00001", content="甲" * 80)},
        {"chunk": make_chunk("pub_d1_00002", content="乙" * 80)},
    ]}]
    limited = review._budget_sections(sections, max_chars=150)
    assert len(limited[0]["chunks"]) == 1
    assert len(review._sections_text(limited)) <= 150


async def test_outline_representatives_never_fall_back_to_other_chapter(monkeypatch):
    other = make_chunk("pub_d1_00001", chapter_id=2)

    async def fake_retrieve(*_args):
        return [{"chunk": other, "score": 0.1}], None

    monkeypatch.setattr(review, "retrieve", fake_retrieve)
    course = SimpleNamespace(id=5, scope="public")
    chapter = SimpleNamespace(id=1, title="第一章")
    assert await review._chapter_reps(None, SimpleNamespace(id=1), course, chapter, 4) == []


async def test_ai_citations_use_snapshot_and_defend_course_scope():
    post = make_post(7, course_id=5)
    post.ai_citation_chunk_ids = ["pub_d1_00001"]
    doc = make_doc(1, scope="public")
    allowed = make_chunk("pub_d1_00001", document_id=1, course_id=5, scope="public")
    outside = make_chunk("pub_d2_00001", document_id=1, course_id=6, scope="public")
    answer = "见 [pub_d1_00001] 与 [pub_d2_00001]。"
    rows = [(allowed, doc, "署名", "章"), (outside, doc, "署名", "章")]
    session = FakeSession(results=[FakeResult(rows=rows)])
    items = await build_ai_citations(session, answer, post=post)
    assert [item["chunk_id"] for item in items] == ["pub_d1_00001"]

    post.ai_citation_chunk_ids = []
    assert await build_ai_citations(session, answer, post=post) == []


def test_bm25_top_ids_keeps_lexical_hits_when_idf_negative():
    """小语料高频词会让 BM25 IDF 为负；只要词面重叠仍应产出候选（v1.3 实跑回归）。"""
    from app.core.retrieval import fine as fine_module

    rows = [
        ("c1", 1, "分页存储管理的页表"),
        ("c2", 1, "分页存储管理的地址转换"),
    ]
    entry = fine_module._build_bm25_entry(rows)
    assert fine_module._bm25_top_ids(entry, "分页存储", None, 5)
    assert fine_module._bm25_top_ids(entry, "完全不相关的词", None, 5) == []


def test_rebuild_defaults_to_dry_run(monkeypatch):
    called = []

    async def fake_dry_run():
        called.append(True)

    monkeypatch.setattr(rebuild_rag_index, "dry_run", fake_dry_run)
    monkeypatch.setattr("sys.argv", ["rebuild_rag_index.py"])
    rebuild_rag_index.main()
    assert called == [True]


async def test_collection_metadata_reads_by_name_on_chroma_1_0(monkeypatch):
    """Chroma 1.0.15 集合详情 GET 只认名称；按 id 会 404（v1.3 重建实跑发现）。"""
    import httpx

    import app.storage.vector_store as vector_store_module
    from app.storage.vector_store import VectorStore

    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "cid-public"})
        if request.url.path.endswith("/chunks_public"):
            return httpx.Response(200, json={"metadata": {"index_version": "rag-structure-v2"}})
        return httpx.Response(404, json={"error": "NotFoundError"})

    store = VectorStore(base_url="http://chroma-test")
    monkeypatch.setitem(
        vector_store_module._clients,
        store.base_url,
        httpx.AsyncClient(base_url=store.base_url, transport=httpx.MockTransport(handler)),
    )
    vector_store_module._collection_ids.clear()
    metadata = await store.get_collection_metadata("chunks_public")
    assert metadata == {"index_version": "rag-structure-v2"}
    assert paths[-1].endswith("/collections/chunks_public")
    assert "cid-public" not in paths[-1]
