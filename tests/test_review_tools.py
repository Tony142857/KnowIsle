"""复习工具接口与页面测试（模块 A4，§12.1）：outline / quiz 的校验、权限、额度、生成与降级。

与 test_api_routes.py 同一约定：FakeSession 按序出队、resolve_client / retrieve
monkeypatch 为内存替身（CI 无 DB / 向量库 / LLM，见 tests/fakestack.py）。
"""

import json
from types import SimpleNamespace

from app.api import review as review_api
from app.storage.models import Chapter, Course, QaLog

from .fakestack import (
    FakeResult,
    FakeSession,
    async_value,
    http_client,
    make_chunk,
    make_course,
    make_user,
)

USER = make_user(1)


class SeqLLM:
    """LLM 客户端替身：chat 按序弹出预置输出；fail=True 模拟 LLM 故障。"""

    def __init__(self, outputs=(), fail=False):
        self.outputs = list(outputs)
        self.fail = fail
        self.prompts = []

    async def chat(self, messages):
        self.prompts.append(messages)
        if self.fail:
            raise RuntimeError("LLM 不可用")
        return self.outputs.pop(0), {"total_tokens": 42}


def _quota(used=0, daily_limit=30, bonus_balance=0):
    return SimpleNamespace(used=used, daily_limit=daily_limit, bonus_balance=bonus_balance)


def _patch_common(monkeypatch, llm, hits, provider="official"):
    """复习路由公共边界：LLM 双通道解析 + 检索。"""
    monkeypatch.setattr(review_api, "resolve_client", async_value((llm, provider)))
    monkeypatch.setattr(review_api, "retrieve", async_value((hits, None)))


def _chapter(chapter_id=1, course_id=5, title="第 1 章 进程"):
    return SimpleNamespace(
        id=chapter_id, course_id=course_id, title=title,
        parent_id=None, order_idx=chapter_id,
    )


def _hits(*chunks):
    return [{"chunk": c, "score": 0.9} for c in chunks]


QUIZ_JSON = json.dumps(
    {
        "questions": [
            {
                "type": "single_choice",
                "question": "进程与线程的区别？",
                "options": ["A. 进程是资源分配单位", "B. 线程是资源分配单位"],
                "answer": "A",
                "explanation": "进程拥有独立地址空间",
                "chunk_id": "per_d11_00001",
            },
            {
                "type": "short_answer",
                "question": "简述死锁的四个必要条件",
                "answer": "互斥、占有且等待、不可剥夺、循环等待",
                "explanation": "",
                "chunk_id": "per_d9_99999",  # 不在上下文内，应被置 None
            },
            {
                "type": "unknown_type",  # 非法类型应规整为 short_answer
                "question": "什么是临界区？",
                "answer": "访问共享资源的代码段",
            },
        ]
    },
    ensure_ascii=False,
)

PERSONAL_GETS = {(Course, 5): make_course(5, scope="personal", owner_id=1)}
PUBLIC_GETS = {(Course, 5): make_course(5, scope="public", status="active")}


# ---------------------------------------------------------------------------
# 请求校验 / 登录 / 权限 / 额度
# ---------------------------------------------------------------------------


def test_quiz_count_validation_422():
    """count 越界（1~10）直接 422，不触达业务逻辑。"""
    with http_client(FakeSession(), USER) as client:
        for count in (0, 11):
            resp = client.post(
                "/api/review/quiz",
                json={"course_id": 5, "chapter_id": 1, "count": count},
            )
            assert resp.status_code == 422


def test_review_endpoints_require_login_401():
    with http_client(FakeSession(), None) as client:
        assert client.post("/api/review/outline", json={"course_id": 5}).status_code == 401
        resp = client.post("/api/review/quiz", json={"course_id": 5, "chapter_id": 1})
        assert resp.status_code == 401


def test_outline_course_permission_404(monkeypatch):
    """他人个人课程 / 未开放公共课程：统一 404，不暴露存在性（§3.2）。"""
    _patch_common(monkeypatch, SeqLLM(), [], provider="user_custom")
    for gets in (
        {},  # 课程不存在
        {(Course, 5): make_course(5, scope="personal", owner_id=9)},  # 他人个人课程
        {(Course, 5): make_course(5, status="pending")},  # 公共课程未开放
    ):
        with http_client(FakeSession(gets=gets), USER) as client:
            assert client.post("/api/review/outline", json={"course_id": 5}).status_code == 404


def test_outline_chapter_ids_not_in_course_404(monkeypatch):
    """chapter_ids 含不属于本课程的章节：404（与越权同一约定）。"""
    _patch_common(monkeypatch, SeqLLM(), [], provider="user_custom")
    session = FakeSession(gets=PERSONAL_GETS, results=[FakeResult(rows=[_chapter(1)])])
    with http_client(session, USER) as client:
        resp = client.post(
            "/api/review/outline", json={"course_id": 5, "chapter_ids": [1, 99]}
        )
    assert resp.status_code == 404


def test_quiz_chapter_mismatch_404(monkeypatch):
    """章节不存在或不属于该课程：404。"""
    _patch_common(monkeypatch, SeqLLM(), [], provider="user_custom")
    with http_client(FakeSession(gets=PERSONAL_GETS), USER) as client:
        resp = client.post("/api/review/quiz", json={"course_id": 5, "chapter_id": 1})
        assert resp.status_code == 404
    gets = {**PERSONAL_GETS, (Chapter, 1): _chapter(1, course_id=99)}
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post("/api/review/quiz", json={"course_id": 5, "chapter_id": 1})
        assert resp.status_code == 404


def test_outline_quota_exceeded_429(monkeypatch):
    """官方通道额度用尽：进入检索前 429（§10.1）。"""
    monkeypatch.setattr(
        review_api, "resolve_client", async_value((SeqLLM(), "official"))
    )
    exhausted = _quota(used=30, daily_limit=30)
    session = FakeSession(results=[FakeResult(scalar=exhausted)])
    with http_client(session, USER) as client:
        resp = client.post("/api/review/outline", json={"course_id": 5})
    assert resp.status_code == 429
    assert "额度" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 复习大纲
# ---------------------------------------------------------------------------


def test_outline_success_with_quota_and_qa_log(monkeypatch):
    """正常路径：按章聚合代表 chunks → 大纲；qa_logs 落库 + 官方额度扣减。"""
    quota = _quota()
    llm = SeqLLM(["# 复习大纲\n- 核心考点：进程调度"])
    chunks = [make_chunk("per_d11_00001", chapter_id=1), make_chunk("per_d11_00002", chapter_id=2)]
    _patch_common(monkeypatch, llm, _hits(*chunks))
    session = FakeSession(gets=PERSONAL_GETS, results=[
        FakeResult(scalar=quota),                       # check_quota
        FakeResult(rows=[_chapter(1), _chapter(2, title="第 2 章 内存")]),  # 章节列表
        FakeResult(scalar=quota),                       # consume_quota
    ])
    with http_client(session, USER) as client:
        resp = client.post("/api/review/outline", json={"course_id": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["outline"].startswith("# 复习大纲")
    assert [c["title"] for c in body["chapters"]] == ["第 1 章 进程", "第 2 章 内存"]
    assert body["token_usage"] == 42 and body["provider"] == "official"
    assert "第 1 章 进程" in llm.prompts[0][0]["content"]  # 章节内容进入 Prompt
    log = next(o for o in session.added if isinstance(o, QaLog))
    assert log.question == "[review] 复习大纲：操作系统"
    assert log.search_scope == "personal" and log.model_provider == "official"
    assert log.top_chunks == ["per_d11_00001", "per_d11_00002"]
    assert quota.used == 1 and session.commits == 1


def test_outline_no_content_422_and_llm_failure_502(monkeypatch):
    """无可用资料 422（不调 LLM）；LLM 故障 502 降级。"""
    llm = SeqLLM(["unused"])
    _patch_common(monkeypatch, llm, [], provider="user_custom")
    session = FakeSession(gets=PERSONAL_GETS, results=[FakeResult(rows=[_chapter(1)])])
    with http_client(session, USER) as client:
        resp = client.post("/api/review/outline", json={"course_id": 5})
    assert resp.status_code == 422 and "资料" in resp.json()["detail"]
    assert llm.prompts == []

    _patch_common(monkeypatch, SeqLLM(fail=True), _hits(make_chunk(chapter_id=1)),
                  provider="user_custom")
    session = FakeSession(gets=PERSONAL_GETS, results=[FakeResult(rows=[_chapter(1)])])
    with http_client(session, USER) as client:
        resp = client.post("/api/review/outline", json={"course_id": 5})
    assert resp.status_code == 502
    assert "AI 服务暂时不可用" in resp.json()["detail"]
    assert not [o for o in session.added if isinstance(o, QaLog)]  # 失败不落库


# ---------------------------------------------------------------------------
# 考点习题
# ---------------------------------------------------------------------------


def test_quiz_success_normalizes_questions_and_logs(monkeypatch):
    """正常路径：结构化习题（选项/答案/解析/关联 chunk_id），非法字段规整。"""
    quota = _quota()
    llm = SeqLLM([QUIZ_JSON])
    _patch_common(monkeypatch, llm, _hits(make_chunk("per_d11_00001", chapter_id=1)))
    gets = {**PERSONAL_GETS, (Chapter, 1): _chapter(1)}
    session = FakeSession(gets=gets, results=[
        FakeResult(scalar=quota),  # check_quota
        FakeResult(scalar=quota),  # consume_quota
    ])
    with http_client(session, USER) as client:
        resp = client.post(
            "/api/review/quiz", json={"course_id": 5, "chapter_id": 1, "count": 3}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["chapter"] == {"id": 1, "title": "第 1 章 进程"}
    questions = body["questions"]
    assert len(questions) == 3
    q0 = questions[0]
    assert q0["type"] == "single_choice" and len(q0["options"]) == 2
    assert q0["answer"] == "A" and q0["chunk_id"] == "per_d11_00001"
    assert questions[1]["chunk_id"] is None  # 幻觉 chunk_id 被剔除
    assert questions[2]["type"] == "short_answer"  # 非法类型规整
    log = next(o for o in session.added if isinstance(o, QaLog))
    assert log.question.startswith("[review] 习题生成：")
    assert json.loads(log.answer)[0]["question"] == "进程与线程的区别？"
    assert quota.used == 1


def test_quiz_invalid_json_retries_once_then_ok(monkeypatch):
    """LLM 首次输出非法 JSON：带提示重试一次成功（messages 含重试提示）。"""
    llm = SeqLLM(["这绝对不是 JSON", QUIZ_JSON])
    _patch_common(monkeypatch, llm, _hits(make_chunk(chapter_id=1)),
                  provider="user_custom")
    gets = {**PERSONAL_GETS, (Chapter, 1): _chapter(1)}
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post("/api/review/quiz", json={"course_id": 5, "chapter_id": 1})
    assert resp.status_code == 200
    assert len(resp.json()["questions"]) == 3
    assert len(llm.prompts) == 2
    assert "合法 JSON" in llm.prompts[1][-1]["content"]


def test_quiz_invalid_json_twice_502_and_llm_failure_502(monkeypatch):
    """重试后仍非法 JSON：明确 502；LLM 调用异常：502 降级。"""
    llm = SeqLLM(["bad", "still bad"])
    _patch_common(monkeypatch, llm, _hits(make_chunk(chapter_id=1)),
                  provider="user_custom")
    gets = {**PERSONAL_GETS, (Chapter, 1): _chapter(1)}
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post("/api/review/quiz", json={"course_id": 5, "chapter_id": 1})
    assert resp.status_code == 502
    assert "格式异常" in resp.json()["detail"]
    assert len(llm.prompts) == 2  # 仅重试一次

    _patch_common(monkeypatch, SeqLLM(fail=True), _hits(make_chunk(chapter_id=1)),
                  provider="user_custom")
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post("/api/review/quiz", json={"course_id": 5, "chapter_id": 1})
    assert resp.status_code == 502
    assert "AI 服务暂时不可用" in resp.json()["detail"]


def test_quiz_empty_chapter_content_422(monkeypatch):
    _patch_common(monkeypatch, SeqLLM(), [], provider="user_custom")
    gets = {**PERSONAL_GETS, (Chapter, 1): _chapter(1)}
    with http_client(FakeSession(gets=gets), USER) as client:
        resp = client.post("/api/review/quiz", json={"course_id": 5, "chapter_id": 1})
    assert resp.status_code == 422 and "资料" in resp.json()["detail"]


def test_quiz_public_course_any_logged_in_user(monkeypatch):
    """公共课程（active）：任何登录用户可生成，非课程所有者也行（模块 A4）。"""
    _patch_common(monkeypatch, SeqLLM([QUIZ_JSON]), _hits(make_chunk(chapter_id=1)),
                  provider="user_custom")
    gets = {**PUBLIC_GETS, (Chapter, 1): _chapter(1)}
    with http_client(FakeSession(gets=gets), make_user(9)) as client:
        resp = client.post("/api/review/quiz", json={"course_id": 5, "chapter_id": 1})
    assert resp.status_code == 200
    assert resp.json()["provider"] == "user_custom"


# ---------------------------------------------------------------------------
# 页面路由（/library/review）
# ---------------------------------------------------------------------------


def test_review_page_login_redirect_and_render():
    """未登录 303 重定向 /login；登录后骨架 200（数据由前端 fetch）。"""
    with http_client(FakeSession(), None) as client:
        resp = client.get("/library/review", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
    with http_client(FakeSession(), USER) as client:
        resp = client.get("/library/review", params={"course_id": 5})
    assert resp.status_code == 200
    assert "复习工具" in resp.text and "reviewPage(5)" in resp.text
