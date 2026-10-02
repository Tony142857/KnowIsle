"""章节树 AI 校验测试（模块 A1「自动解析 + AI 校验」附加层）。

覆盖：样本提取与结论解析（纯函数）、LLM 正常校验路径、LLM 异常/脏输出降级路径、
以及解析 worker 集成（校验任何结果都不影响 doc.status 与切块落库）。
LLM 与解析流水线外部边界均为内存替身，模式同 tests/test_workers.py。
"""

import logging
from types import SimpleNamespace

import pytest

from app.core.parser.base import ParsedBlock
from app.core.structure import ai_verifier
from app.storage.models import Chunk, Document
from app.workers import parse_worker
from app.workers.parse_worker import parse_document

from .fakestack import (
    FakeEmbedding,
    FakeLLM,
    FakeObjectStore,
    FakeResult,
    FakeSession,
    FakeVectorStore,
    async_value,
    make_doc,
    session_local,
)


def _blocks():
    return [
        ParsedBlock(title="第一章 操作系统引论", content="", level=1, page_num=1),
        ParsedBlock(title=None, content="操作系统是管理计算机硬件与软件资源的程序。", level=0, page_num=1),
        ParsedBlock(title="第二章 进程管理", content="", level=1, page_num=10),
        ParsedBlock(title=None, content="进程是资源分配的最小单位。", level=0, page_num=10),
    ]


# ---------------------------------------------------------------------------
# 纯函数：样本提取 / 结论解析
# ---------------------------------------------------------------------------


def test_collect_tree_sample_groups_content_by_chapter():
    sample = ai_verifier.collect_tree_sample(_blocks())
    assert [c["title"] for c in sample] == ["第一章 操作系统引论", "第二章 进程管理"]
    assert "管理计算机硬件" in sample[0]["sample"]
    assert "资源分配的最小单位" in sample[1]["sample"]


def test_collect_tree_sample_ignores_preface_and_truncates():
    blocks = [
        ParsedBlock(title=None, content="封面页正文，不属于任何章节", level=0),
        ParsedBlock(title="唯一章", content="", level=1),
        ParsedBlock(title=None, content="长" * 500, level=0),
        ParsedBlock(title="1.1 小节不算章", content="小节正文", level=2),  # level=2 不建章
    ]
    sample = ai_verifier.collect_tree_sample(blocks)
    assert len(sample) == 1 and sample[0]["title"] == "唯一章"
    assert len(sample[0]["sample"]) == ai_verifier._SAMPLE_CHARS
    assert "封面页" not in sample[0]["sample"]


def test_parse_verify_response_tolerates_prose_and_fence():
    text = (
        '好的，结论如下：```json\n'
        '{"ok": false, "issues": ["第 3 章疑似正文误判为标题"], "suggestions": ["合并相邻短章"]}\n'
        '```以上供参考'
    )
    result = ai_verifier.parse_verify_response(text)
    assert result == {
        "ok": False,
        "issues": ["第 3 章疑似正文误判为标题"],
        "suggestions": ["合并相邻短章"],
    }


def test_parse_verify_response_defaults_and_rejects_garbage():
    # ok 缺省时按 issues 是否为空推断
    assert ai_verifier.parse_verify_response('{"issues": []}')["ok"] is True
    assert ai_verifier.parse_verify_response('{"issues": ["层级错乱"]}')["ok"] is False
    with pytest.raises(ValueError, match="不含 JSON"):
        ai_verifier.parse_verify_response("章节树看起来很合理，没有问题。")


# ---------------------------------------------------------------------------
# LLM 校验路径：正常 / 降级
# ---------------------------------------------------------------------------


async def test_verify_tree_ok_path_sends_titles_and_samples():
    llm = FakeLLM('{"ok": true, "issues": [], "suggestions": []}')
    result = await ai_verifier.verify_tree(llm, ai_verifier.collect_tree_sample(_blocks()))
    assert result == {"ok": True, "issues": [], "suggestions": []}
    assert "第一章 操作系统引论" in llm.prompts[0]  # 提示词含章节标题与正文样本
    assert "资源分配的最小单位" in llm.prompts[0]


async def test_verify_tree_safe_llm_failure_falls_back(caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.structure.ai_verifier"):
        result = await ai_verifier.verify_tree_safe(_blocks(), document_id=5, client=FakeLLM(fail=True))
    # 降级为「规则结果直接采用」：ok=True + fallback 原因，绝不抛出
    assert result["ok"] is True and result["issues"] == []
    assert result["fallback"] == "LLM 不可用"
    assert "降级为规则结果直接采用" in caplog.text and "document_id=5" in caplog.text


async def test_verify_tree_safe_bad_output_falls_back():
    result = await ai_verifier.verify_tree_safe(_blocks(), document_id=5, client=FakeLLM("无法解析的输出"))
    assert result["ok"] is True and "不含 JSON" in result["fallback"]


async def test_verify_tree_safe_skips_when_no_chapters():
    llm = FakeLLM(fail=True)
    blocks = [ParsedBlock(title=None, content="通篇正文无标题", level=0)]
    result = await ai_verifier.verify_tree_safe(blocks, document_id=5, client=llm)
    assert result == {"ok": True, "issues": [], "suggestions": [], "fallback": "no_chapters"}
    assert llm.prompts == []  # 无章节不发起 LLM 调用


async def test_verify_tree_safe_logs_issues_for_manual_review(caplog):
    llm = FakeLLM('{"ok": false, "issues": ["章节切分过碎"], "suggestions": ["合并第 2-4 章"]}')
    with caplog.at_level(logging.WARNING, logger="app.core.structure.ai_verifier"):
        result = await ai_verifier.verify_tree_safe(_blocks(), document_id=5, client=llm)
    assert result["ok"] is False and result["issues"] == ["章节切分过碎"]
    assert "供人工修正参考" in caplog.text and "章节切分过碎" in caplog.text


# ---------------------------------------------------------------------------
# 解析 worker 集成：校验任何结果都不影响解析主流程
# ---------------------------------------------------------------------------


def _patch_parse_pipeline(monkeypatch, session):
    """解析流水线全部外部边界换成内存替身（同 test_workers.py 模式）。"""
    monkeypatch.setattr(parse_worker, "SessionLocal", session_local(session))
    monkeypatch.setattr(parse_worker, "object_store", FakeObjectStore({"k/5": b"pdf-bytes"}))
    monkeypatch.setattr(parse_worker, "parse_by_file_type", lambda _ft, _data: _blocks())
    monkeypatch.setattr(parse_worker, "tree_builder",
                        SimpleNamespace(build_tree=async_value([10, 10, 11, 11])))
    monkeypatch.setattr(
        parse_worker, "semantic_splitter",
        SimpleNamespace(split_blocks=lambda *_a: [
            {"chunk_id": "pub_d5_00001", "chapter_id": 10, "page_num": 1,
             "section": "1.1", "content": "第一段"},
        ]),
    )
    monkeypatch.setattr(parse_worker, "get_embedding", FakeEmbedding)
    monkeypatch.setattr(parse_worker, "get_vector_store", lambda: FakeVectorStore())
    monkeypatch.setattr(parse_worker, "invalidate_bm25", lambda *args: None)


async def test_parse_document_runs_verifier_and_logs_issues(monkeypatch, caplog):
    doc = make_doc(doc_id=5, scope="public", status="parsing")
    session = FakeSession(gets={(Document, 5): doc}, results=[FakeResult()])
    _patch_parse_pipeline(monkeypatch, session)
    llm = FakeLLM('{"ok": false, "issues": ["章节切分过碎"], "suggestions": []}')
    monkeypatch.setattr(ai_verifier, "get_official_client", lambda _tier: llm)
    with caplog.at_level(logging.WARNING, logger="app.core.structure.ai_verifier"):
        await parse_document({}, 5)
    assert doc.status == "parsed" and session.commits == 1  # 主流程照常成功
    assert [o.chunk_id for o in session.added if isinstance(o, Chunk)] == ["pub_d5_00001"]
    assert len(llm.prompts) == 1  # 校验确实执行
    assert "章节切分过碎" in caplog.text  # 结论落日志


async def test_parse_document_llm_failure_still_parsed(monkeypatch, caplog):
    doc = make_doc(doc_id=5, scope="public", status="parsing")
    session = FakeSession(gets={(Document, 5): doc}, results=[FakeResult()])
    _patch_parse_pipeline(monkeypatch, session)
    monkeypatch.setattr(ai_verifier, "get_official_client", lambda _tier: FakeLLM(fail=True))
    with caplog.at_level(logging.WARNING, logger="app.core.structure.ai_verifier"):
        await parse_document({}, 5)  # LLM 故障不抛出、不触发重试
    assert doc.status == "parsed" and session.commits == 1 and session.rollbacks == 0
    assert "降级为规则结果直接采用" in caplog.text
