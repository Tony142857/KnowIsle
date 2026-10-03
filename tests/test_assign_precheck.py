"""v0.9 协审指派与自动预检单元测试：assign 候选选取 / precheck 硬失败链路（§18.1）。

与 test_workflow.py 同一约定：CI 无 DB/Redis/LLM，FakeSession 内存替身复用自
tests/test_workflow.py，LLM 官方客户端 monkeypatch 为 FakeLLM。
"""

import pytest

from app.moderation import precheck
from app.moderation.assign import assign_reviewers
from app.moderation.precheck import _parse_ai_review, run_precheck
from app.storage.models import Chunk
from tests.test_workflow import FakeSession, _course, _doc, _resource, _user

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


class FakeLLM:
    """官方模型客户端替身：固定返回文本或抛错，记录收到的消息。"""

    def __init__(self, text="", exc=None):
        self.text = text
        self.exc = exc
        self.messages: list = []

    async def chat(self, messages):
        if self.exc is not None:
            raise self.exc
        self.messages.extend(messages)
        return self.text, {}


def _reviewer(uid, major_id=1, status="active", role="reviewer"):
    return _user(uid, role=role, status=status, major_id=major_id)


def _patch_llm(monkeypatch, llm):
    monkeypatch.setattr(precheck, "get_official_client", lambda tier: llm)
    return llm


def _seed_precheck_scenario(session, doc_status="parsed", with_chunks=True, title="期末笔记"):
    """预检场景标准数据：原文档 + chunks + 投稿资源 + 课程。"""
    doc = _doc(status=doc_status)
    session.seed(doc, _course())
    if with_chunks:
        session.seed(
            Chunk(
                id=1001, chunk_id="d100_00001", document_id=100, course_id=10,
                owner_id=1, scope="personal", file_type="pdf_textbook", content="内容一",
            ),
            Chunk(
                id=1002, chunk_id="d100_00002", document_id=100, course_id=10,
                owner_id=1, scope="personal", file_type="pdf_textbook", content="内容二",
            ),
        )
    resource = _resource(title=title)
    session.seed(resource)
    return resource


# ---------------------------------------------------------------------------
# assign_reviewers：同专业优先 + 排除投稿人/指定人 + 随机补足
# ---------------------------------------------------------------------------


async def test_assign_prefers_same_major_and_caps_count():
    session = FakeSession()
    session.seed(
        _course(major_id=1),
        _reviewer(1, major_id=1),  # 投稿人本人，排除
        _reviewer(5, major_id=1),
        _reviewer(7, major_id=1),
        _reviewer(6, major_id=2),  # 非同专业，排后
        _reviewer(8, major_id=1, role="student"),  # 非协审员，排除
        _reviewer(9, major_id=1, status="muted"),  # 非 active，排除
    )
    result = await assign_reviewers(session, _resource(), count=2)
    assert set(result) == {5, 7}  # 同专业优先取满 count


async def test_assign_fills_from_other_majors():
    """同专业不足时用其他专业随机补足，同专业仍排前。"""
    session = FakeSession()
    session.seed(
        _course(major_id=1),
        _reviewer(1, major_id=1),  # 投稿人
        _reviewer(5, major_id=1),
        _reviewer(6, major_id=2),
        _reviewer(8, major_id=3),
    )
    result = await assign_reviewers(session, _resource(), count=3)
    assert result[0] == 5 and set(result[1:]) == {6, 8}


async def test_assign_exclude_ids():
    session = FakeSession()
    session.seed(_course(major_id=1), _reviewer(5, major_id=1), _reviewer(6, major_id=2))
    result = await assign_reviewers(session, _resource(), count=5, exclude_ids=[5])
    assert result == [6]


async def test_assign_no_candidates_returns_empty():
    session = FakeSession()
    session.seed(_course(major_id=1), _reviewer(1, major_id=1))  # 仅投稿人本人
    assert await assign_reviewers(session, _resource()) == []


async def test_assign_fewer_candidates_than_count():
    session = FakeSession()
    session.seed(_course(major_id=1), _reviewer(5, major_id=2))
    assert await assign_reviewers(session, _resource(), count=2) == [5]  # 不足则全返回


# ---------------------------------------------------------------------------
# _parse_ai_review：LLM 输出的 JSON 提取（容忍代码围栏与前后噪声）
# ---------------------------------------------------------------------------


def test_parse_ai_review_plain_json():
    assert _parse_ai_review('{"score": 85, "comment": "质量好"}')["score"] == 85


def test_parse_ai_review_fenced_and_noisy():
    text = '分析如下：\n```json\n{"score": 70}\n```\n以上。'
    assert _parse_ai_review(text) == {"score": 70}


def test_parse_ai_review_no_json_raises():
    with pytest.raises(ValueError, match="不含 JSON"):
        _parse_ai_review("无法评价该资料")
    with pytest.raises(ValueError, match="不含 JSON"):
        _parse_ai_review("}{")  # 括号顺序颠倒


# ---------------------------------------------------------------------------
# run_precheck：格式 / 查重 / 敏感词 / AI 初评
# ---------------------------------------------------------------------------


async def test_precheck_pass(monkeypatch):
    llm = _patch_llm(monkeypatch, FakeLLM('{"score": 85, "comment": "内容完整"}'))
    session = FakeSession()
    resource = _seed_precheck_scenario(session)
    result = await run_precheck(session, resource)
    assert result["format_ok"] is True and result["duplicate"] is False
    assert result["sensitive_hits"] == [] and result["hard_fail"] is False
    assert result["reason"] == ""
    assert result["ai_review"]["score"] == 85
    # AI 初评 prompt 带入课程名与资料标题
    assert "操作系统" in llm.messages[0]["content"]
    assert "期末笔记" in llm.messages[0]["content"]


async def test_precheck_format_fail_when_doc_not_parsed(monkeypatch):
    _patch_llm(monkeypatch, FakeLLM('{"score": 50}'))
    session = FakeSession()
    resource = _seed_precheck_scenario(session, doc_status="failed")
    result = await run_precheck(session, resource)
    assert result["format_ok"] is False and result["hard_fail"] is True
    assert "文档未成功解析或无有效内容" in result["reason"]


async def test_precheck_format_fail_when_no_chunks(monkeypatch):
    _patch_llm(monkeypatch, FakeLLM('{"score": 50}'))
    session = FakeSession()
    resource = _seed_precheck_scenario(session, with_chunks=False)
    result = await run_precheck(session, resource)
    assert result["format_ok"] is False and result["hard_fail"] is True


async def test_precheck_duplicate_md5(monkeypatch):
    """同 MD5 且审核中/已上架的其他资源 → 判重硬失败。"""
    _patch_llm(monkeypatch, FakeLLM('{"score": 80}'))
    session = FakeSession()
    resource = _seed_precheck_scenario(session)
    session.seed(
        _doc(did=101, md5="md5-src"),  # 同指纹的另一文档
        _resource(rid=1001, document_id=101, review_status="approved"),
    )
    result = await run_precheck(session, resource)
    assert result["duplicate"] is True and result["hard_fail"] is True
    assert "内容重复" in result["reason"]


async def test_precheck_rejected_same_md5_not_duplicate(monkeypatch):
    """已驳回的同 MD5 资源不算重复（允许修改重投）。"""
    _patch_llm(monkeypatch, FakeLLM('{"score": 80}'))
    session = FakeSession()
    resource = _seed_precheck_scenario(session)
    session.seed(
        _doc(did=101, md5="md5-src"),
        _resource(rid=1001, document_id=101, review_status="rejected"),
    )
    result = await run_precheck(session, resource)
    assert result["duplicate"] is False and result["hard_fail"] is False


async def test_precheck_sensitive_hit_in_title(monkeypatch):
    _patch_llm(monkeypatch, FakeLLM('{"score": 80}'))
    session = FakeSession()
    resource = _seed_precheck_scenario(session, title="包过秘籍")
    result = await run_precheck(session, resource)
    assert result["sensitive_hits"] == ["包过"] and result["hard_fail"] is True
    assert "命中敏感词：包过" in result["reason"]


async def test_precheck_multiple_hard_fail_reasons(monkeypatch):
    """多项硬失败理由用「；」连接（格式 → 重复 → 敏感词顺序）。"""
    _patch_llm(monkeypatch, FakeLLM('{"score": 10}'))
    session = FakeSession()
    resource = _seed_precheck_scenario(session, doc_status="failed", title="包过秘籍")
    session.seed(
        _doc(did=101, md5="md5-src"),
        _resource(rid=1001, document_id=101, review_status="pending"),
    )
    result = await run_precheck(session, resource)
    assert result["hard_fail"] is True
    assert result["reason"] == (
        "文档未成功解析或无有效内容；与审核中/已上架的资料内容重复；命中敏感词：包过"
    )


async def test_precheck_ai_failure_not_blocking(monkeypatch):
    """LLM 异常记入 error，不阻塞审核流程。"""
    _patch_llm(monkeypatch, FakeLLM(exc=RuntimeError("api down")))
    session = FakeSession()
    resource = _seed_precheck_scenario(session)
    result = await run_precheck(session, resource)
    assert result["ai_review"] == {"error": "api down"}
    assert result["hard_fail"] is False


async def test_precheck_ai_invalid_output_not_blocking(monkeypatch):
    """LLM 输出不含 JSON：解析失败记入 error，不阻塞。"""
    _patch_llm(monkeypatch, FakeLLM("我无法评价这份资料"))
    session = FakeSession()
    resource = _seed_precheck_scenario(session)
    result = await run_precheck(session, resource)
    assert "error" in result["ai_review"]
    assert result["hard_fail"] is False
