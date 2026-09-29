"""v0.5 资源克隆单元测试（§12.1）：chunk 重编号与字段映射、幂等判定、课程归属校验。

与 test_community.py / test_growth.py 同一约定：CI 无 DB/Redis/Chroma，
全部为纯函数内存测试（SimpleNamespace 替身）；PG + Chroma + bge 嵌入的
全链路在容器 compose 环境内验证。
"""

from types import SimpleNamespace

import pytest

from app.api.resources import (
    CLONE_COURSE_NAME,
    CloneRequest,
    build_cloned_chunk,
    check_clone_course,
    decide_clone_hit,
    make_personal_chunk_id,
)

# ---------------------------------------------------------------------------
# chunk 重编号与字段映射（app/api/resources.py）
# ---------------------------------------------------------------------------


def test_make_personal_chunk_id():
    assert make_personal_chunk_id(42, 1) == "per_d42_00001"
    assert make_personal_chunk_id(42, 12345) == "per_d42_12345"
    assert make_personal_chunk_id(7, 9) == "per_d7_00009"


def test_build_cloned_chunk_mapping():
    # 源为公共副本 chunk：克隆后 chapter_id 置空、归属改为本人个人课程，其余照抄
    src = SimpleNamespace(
        chunk_id="pub_d3_00002",
        section="1.2 死锁",
        page_num=17,
        file_type="pdf_textbook",
        content="死锁的四个必要条件……",
    )
    d = build_cloned_chunk(src, new_doc_id=9, course_id=5, owner_id=33, seq=2)
    assert d["chunk_id"] == "per_d9_00002"
    assert d["embedding_id"] == d["chunk_id"]
    assert d["document_id"] == 9
    assert d["course_id"] == 5
    assert d["owner_id"] == 33
    assert d["scope"] == "personal"
    assert d["chapter_id"] is None
    assert d["section"] == "1.2 死锁"
    assert d["page_num"] == 17
    assert d["file_type"] == "pdf_textbook"
    assert d["content"] == "死锁的四个必要条件……"


def test_build_cloned_chunk_seq_keeps_order():
    # seq 从 1 递增即保持源 chunks 按 Chunk.id 排序后的原顺序
    src = SimpleNamespace(section=None, page_num=None, file_type="markdown", content="c")
    ids = [build_cloned_chunk(src, 1, 1, 1, seq)["chunk_id"] for seq in (1, 2, 3)]
    assert ids == ["per_d1_00001", "per_d1_00002", "per_d1_00003"]


# ---------------------------------------------------------------------------
# 幂等判定（decide_clone_hit）
# ---------------------------------------------------------------------------


def test_decide_clone_hit_none():
    assert decide_clone_hit(None) is None


def test_decide_clone_hit_existing():
    # 本人已有同 storage_key 文档 → 返回该文档信息的幂等响应体
    doc = SimpleNamespace(id=11, course_id=5)
    assert decide_clone_hit(doc) == {"document_id": 11, "course_id": 5, "cloned": False}


# ---------------------------------------------------------------------------
# 课程归属校验（check_clone_course）
# ---------------------------------------------------------------------------


def test_check_clone_course_ok():
    course = SimpleNamespace(scope="personal", owner_id=7)
    assert check_clone_course(course, 7) is None


def test_check_clone_course_rejects():
    # 不存在 / 公共课程 / 他人个人课程 三种分支均映射 404
    with pytest.raises(ValueError, match="not_found"):
        check_clone_course(None, 7)
    with pytest.raises(ValueError, match="not_found"):
        check_clone_course(SimpleNamespace(scope="public", owner_id=None), 7)
    with pytest.raises(ValueError, match="not_found"):
        check_clone_course(SimpleNamespace(scope="personal", owner_id=8), 7)


# ---------------------------------------------------------------------------
# 请求模型与常量契约
# ---------------------------------------------------------------------------


def test_clone_request_and_default_course_name():
    assert CloneRequest().course_id is None
    assert CloneRequest(course_id=3).course_id == 3
    assert CLONE_COURSE_NAME == "公共库克隆"
