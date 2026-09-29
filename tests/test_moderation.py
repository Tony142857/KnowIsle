"""v0.4 三级审核单元测试（§18.1）：敏感词 / 硬失败判定 / 协审多数决 / 投稿与评分契约。

与 test_identity.py 同一约定：CI 无 DB/Redis/Chroma/LLM，全部为纯函数与
pydantic 模型校验的内存测试；审核全链路在容器 compose 环境内验证。
"""

import pytest
from pydantic import ValidationError

from app.api.documents import SubmitRequest
from app.api.resources import RatingRequest
from app.api.review_tasks import VerdictRequest
from app.core.llm.prompts import PRECHECK_PROMPT
from app.moderation.precheck import decide_hard_fail
from app.moderation.sensitive_words import find_sensitive_hits
from app.moderation.workflow import decide_co_outcome, make_public_chunk_id


def test_find_sensitive_hits_single():
    assert find_sensitive_hits("这份资料包过，放心买") == ["包过"]


def test_find_sensitive_hits_order_and_dedup():
    # 多词命中：按在文本中的出现顺序返回，重复出现不重复列出
    hits = find_sensitive_hits("枪支弹药与代考服务，代考包过")
    assert hits == ["枪支", "代考", "包过"]


def test_find_sensitive_hits_none():
    assert find_sensitive_hits("操作系统期末复习笔记") == []


def test_decide_hard_fail_all_pass():
    assert decide_hard_fail(True, False, []) == (False, "")


def test_decide_hard_fail_single_reason():
    assert decide_hard_fail(False, False, []) == (True, "文档未成功解析或无有效内容")
    assert decide_hard_fail(True, True, []) == (True, "与审核中/已上架的资料内容重复")
    assert decide_hard_fail(True, False, ["代考"]) == (True, "命中敏感词：代考")


def test_decide_hard_fail_multiple_reasons():
    # 多项不通过：理由用「；」连接（格式 → 重复 → 敏感词固定顺序）
    hard_fail, reason = decide_hard_fail(False, True, ["代考", "包过"])
    assert hard_fail
    assert reason == "文档未成功解析或无有效内容；与审核中/已上架的资料内容重复；命中敏感词：代考、包过"


def test_decide_co_outcome_waiting():
    # 人数未齐：继续等待，不出结论
    assert decide_co_outcome([], needed=2) is None
    assert decide_co_outcome(["approve"], needed=2) is None


def test_decide_co_outcome_majority():
    # 驳回票过半才 reject，否则 approve
    assert decide_co_outcome(["approve", "reject"], needed=2) == "approve"  # 2 人 1 驳未过半
    assert decide_co_outcome(["reject", "reject"], needed=2) == "reject"  # 2 人全驳
    assert decide_co_outcome(["reject", "reject", "approve"], needed=3) == "reject"  # 3 人 2 驳
    assert decide_co_outcome(["approve", "approve", "reject"], needed=3) == "approve"  # 3 人 1 驳


def test_precheck_prompt_placeholder():
    # 防止 PRECHECK_PROMPT 占位符更名回归（JSON braces 已转义，仅此一个占位符）
    rendered = PRECHECK_PROMPT.format(document_text_head="测试内容")
    assert "测试内容" in rendered


def test_submit_request_validation():
    req = SubmitRequest(title="计网笔记", course_id=1)
    assert req.description is None and req.chapter_id is None
    with pytest.raises(ValidationError):
        SubmitRequest(title="", course_id=1)


def test_rating_request_bounds():
    assert RatingRequest(stars=1).stars == 1
    assert RatingRequest(stars=5).stars == 5
    for bad in (0, 6):
        with pytest.raises(ValidationError):
            RatingRequest(stars=bad)


def test_verdict_request_literal():
    assert VerdictRequest(verdict="approve").comment is None
    with pytest.raises(ValidationError):
        VerdictRequest(verdict="pass")


def test_make_public_chunk_id():
    # 派生 public 副本的 chunk 业务 ID 契约（与 semantic_splitter 的 pub_d 约定一致）
    assert make_public_chunk_id(45, 7) == "pub_d45_00007"
