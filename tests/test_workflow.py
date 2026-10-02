"""v0.9 审核状态机单元测试：workflow 三级流转 / 协审多数决 / 派生 public 副本（§18.1）。

与 test_growth.py 同一约定：CI 无 DB/Redis/Chroma/LLM/对象存储，session 为内存替身
（FakeSession 内置极简 where 求值器，仅支持本模块用到的等值/不等/in/not_in/and
与单列排序，遇到不支持的查询直接断言失败）；向量/embed/BM25 失效/协审指派全部
monkeypatch。被测函数契约是「只改 session 不 commit」——FakeSession 不提供
commit 方法，若被测代码违规调用会直接 AttributeError。
"""

from collections import defaultdict
from types import SimpleNamespace

import pytest
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, BooleanClauseList

from app.identity import growth
from app.identity.growth import SCORE_UPLOAD_APPROVED
from app.moderation import workflow
from app.moderation.workflow import (
    apply_precheck_result,
    create_submission,
    direct_verdict,
    final_verdict,
    submit_co_verdict,
)
from app.storage.models import (
    Chunk,
    Course,
    Document,
    Follow,
    Notification,
    Resource,
    ReviewRecord,
    ReviewTask,
    ScoreLog,
    User,
)

# ---------------------------------------------------------------------------
# 内存替身
# ---------------------------------------------------------------------------


class FakeResult:
    """极简 Result 替身：scalars/first/all 三种消费方式。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


def _column_ref(expr):
    """where 条件的左值列 → (表名, 属性名)（兼容 InstrumentedAttribute 与 Column）。"""
    owner = getattr(expr, "class_", None)
    if owner is not None:
        return owner.__tablename__, expr.key
    return expr.table.name, expr.key


def _eval_clause(clause, ctx):
    """对 ctx 中的内存对象求值单条 where 条件；不支持的形态直接断言失败。"""
    if isinstance(clause, BooleanClauseList):
        values = [_eval_clause(c, ctx) for c in clause.clauses]
        if clause.operator is operators.and_:
            return all(values)
        return any(values)
    if isinstance(clause, BinaryExpression):
        table, key = _column_ref(clause.left)
        right = clause.right
        right_val = right.value if isinstance(right, BindParameter) else right
        left_val = getattr(ctx[table], key)
        # in_op/notin_op 是 SQLAlchemy 包装的操作符（期望左侧为 SQL 元素），就地求值
        if clause.operator is operators.in_op:
            return left_val in right_val
        if clause.operator is operators.notin_op:
            return left_val not in right_val
        return clause.operator(left_val, right_val)
    raise AssertionError(f"FakeSession 不支持的查询条件: {clause!r}")


class FakeSession:
    """极简 AsyncSession 内存替身：add/add_all/get/execute/flush（无 commit）。"""

    def __init__(self):
        self.added: list = []
        self._seq = defaultdict(int)
        self.users: list = []
        self.courses: list = []
        self.documents: list = []
        self.chunks: list = []
        self.resources: list = []
        self.tasks: list = []
        self.records: list = []
        self.follows: list = []
        self.notifications: list = []
        self.score_logs: list = []

    def _table(self, model):
        return {
            User: self.users,
            Course: self.courses,
            Document: self.documents,
            Chunk: self.chunks,
            Resource: self.resources,
            ReviewTask: self.tasks,
            ReviewRecord: self.records,
            Follow: self.follows,
            Notification: self.notifications,
            ScoreLog: self.score_logs,
        }[model]

    def seed(self, *objs):
        """预置已有数据（不计入 added，便于断言被测函数新增了什么）。"""
        for obj in objs:
            self._register(obj)

    def _register(self, obj):
        self._table(type(obj)).append(obj)
        oid = getattr(obj, "id", None)
        if oid is None:
            self._seq[type(obj)] += 1
            obj.id = self._seq[type(obj)]
        else:
            self._seq[type(obj)] = max(self._seq[type(obj)], oid)  # 模拟自增列：新 id 总大于已有

    def add(self, obj):
        self.added.append(obj)
        self._register(obj)

    def add_all(self, objs):
        for obj in objs:
            self.add(obj)

    async def flush(self):
        pass  # id 在 _register 时已分配

    async def get(self, model, pk):
        return next((o for o in self._table(model) if getattr(o, "id", None) == pk), None)

    def _candidates(self, entity, is_full_entity, column_name):
        if entity is Resource and not is_full_entity:
            # select(Resource.id).join(Document, ...)：MD5 查重，按 document_id 配对
            pairs = []
            for r in self.resources:
                doc = next((d for d in self.documents if d.id == r.document_id), None)
                pairs.append((r.id, {"resources": r, "documents": doc}))
            return pairs
        name = entity.__tablename__
        if is_full_entity:
            return [(obj, {name: obj}) for obj in self._table(entity)]
        # 单列查询（如 select(Follow.user_id)）：值取该列属性
        return [(getattr(obj, column_name), {name: obj}) for obj in self._table(entity)]

    def _order_key(self, stmt, ctx):
        clauses = stmt._order_by_clauses
        if not clauses:
            return 0
        table, key = _column_ref(clauses[0])
        return getattr(ctx[table], key)

    async def execute(self, stmt):
        cd = stmt.column_descriptions[0]
        entity = cd["entity"]
        is_full_entity = cd["name"] == entity.__name__
        matched = [
            (value, ctx)
            for value, ctx in self._candidates(entity, is_full_entity, cd["name"])
            if all(_eval_clause(c, ctx) for c in stmt._where_criteria)
        ]
        if is_full_entity:
            matched.sort(key=lambda p: self._order_key(stmt, p[1]))
        return FakeResult([value for value, _ in matched])


class FakeZRedis:
    """最小内存 Redis 替身：zadd/zincrby（Sorted Set 用 dict 模拟）。"""

    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zincrby(self, key, amount, member):
        zset = self.zsets.setdefault(key, {})
        zset[member] = zset.get(member, 0) + amount
        return zset[member]


class FakeEmbedding:
    """记录每批输入的 embed 替身，向量内容无意义。"""

    def __init__(self):
        self.batches: list[list[str]] = []

    async def embed(self, texts):
        self.batches.append(list(texts))
        return [[float(len(t)), 0.0] for t in texts]


class FakeVectorStore:
    """记录 collection 与 upsert 调用的向量库替身。"""

    def __init__(self):
        self.collections: list[str] = []
        self.upserts: list[dict] = []

    async def get_or_create_collection(self, name):
        self.collections.append(name)
        return f"coll-{name}"

    async def upsert(self, collection, ids, embeddings, metadatas, documents):
        self.upserts.append(
            {
                "collection": collection,
                "ids": list(ids),
                "embeddings": list(embeddings),
                "metadatas": list(metadatas),
                "documents": list(documents),
            }
        )


# ---------------------------------------------------------------------------
# 对象构造辅助
# ---------------------------------------------------------------------------


def _user(uid, role="student", status="active", major_id=1, score=0, level=1):
    return User(
        id=uid,
        student_no=f"2023{uid:04d}",
        real_name=f"用户{uid}",
        nickname=f"user{uid}",
        role=role,
        status=status,
        major_id=major_id,
        score=score,
        level=level,
        credit=100,
    )


def _course(cid=10, major_id=1, name="操作系统"):
    return Course(id=cid, major_id=major_id, scope="public", name=name, status="active")


def _doc(did=100, course_id=10, owner_id=1, status="parsed", md5="md5-src"):
    return Document(
        id=did,
        course_id=course_id,
        owner_id=owner_id,
        scope="personal",
        file_name=f"笔记{did}.pdf",
        file_type="pdf_textbook",
        storage_key=f"docs/{did}.pdf",
        md5=md5,
        status=status,
    )


def _resource(
    rid=1000,
    document_id=100,
    course_id=10,
    uploader_id=1,
    title="期末笔记",
    review_status="pending",
    chapter_id=500,
):
    return Resource(
        id=rid,
        document_id=document_id,
        course_id=course_id,
        uploader_id=uploader_id,
        title=title,
        description="描述",
        chapter_id=chapter_id,
        review_status=review_status,
    )


def _task(tid=2000, resource_id=1000, stage="precheck", assignee_ids=None):
    return ReviewTask(id=tid, resource_id=resource_id, stage=stage, assignee_ids=assignee_ids)


def _chunk(cid, document_id=100, content="内容", page_num=1, section="第一章"):
    return Chunk(
        id=cid,
        chunk_id=f"d{document_id}_{cid:05d}",
        document_id=document_id,
        course_id=10,
        chapter_id=None,
        owner_id=1,
        scope="personal",
        section=section,
        page_num=page_num,
        file_type="pdf_textbook",
        content=content,
    )


def _record(rid, task_id=2000, reviewer_id=5, stage="co_review", verdict="approve", comment=None):
    return ReviewRecord(
        id=rid, task_id=task_id, reviewer_id=reviewer_id,
        stage=stage, verdict=verdict, comment=comment,
    )


def _notifications(session, type_=None):
    return [n for n in session.notifications if type_ is None or n.type == type_]


def _patch_external(monkeypatch):
    """切断 embed / 向量库 / BM25 / Redis 外部依赖，返回各替身供断言。"""
    embedding, store, redis = FakeEmbedding(), FakeVectorStore(), FakeZRedis()
    bm25_calls: list[tuple] = []
    monkeypatch.setattr(workflow, "get_embedding", lambda: embedding)
    monkeypatch.setattr(workflow, "get_vector_store", lambda: store)
    monkeypatch.setattr(workflow, "invalidate_bm25", lambda *a: bm25_calls.append(a))
    monkeypatch.setattr(growth, "get_redis", lambda: redis)
    return embedding, store, redis, bm25_calls


def _seed_approve_scenario(session, with_chunks=True, with_followers=True, with_course=True):
    """终审通过场景的标准数据：投稿人/课程/原文档/chunks/资源/final 任务/关注者。"""
    uploader = _user(1, score=0)
    session.seed(uploader)
    if with_course:
        session.seed(_course())
    session.seed(_doc())
    if with_chunks:
        session.seed(
            _chunk(1001, content="内容一", page_num=3),
            _chunk(1002, content="内容二", page_num=4),
            _chunk(1003, content="内容三", page_num=None),
        )
    resource = _resource(review_status="co_reviewing")
    task = _task(stage="final", assignee_ids=[5, 6])
    session.seed(resource, task)
    if with_followers:
        session.seed(
            Follow(id=3001, user_id=9, target_type="course", target_id=10),
            Follow(id=3002, user_id=1, target_type="course", target_id=10),  # 投稿人本人，不通知
            Follow(id=3003, user_id=8, target_type="course", target_id=99),  # 别的课程
            Follow(id=3004, user_id=7, target_type="user", target_id=10),  # 关注的是用户
        )
    return uploader, resource, task


# ---------------------------------------------------------------------------
# create_submission：投稿建档与重复投稿契约
# ---------------------------------------------------------------------------


async def test_create_submission_new():
    session = FakeSession()
    doc, course, chapter = _doc(), _course(), SimpleNamespace(id=500)
    session.seed(course)
    resource, task, is_resubmit = await create_submission(
        session, _user(1), doc, course, chapter, "期末笔记", "描述"
    )
    assert is_resubmit is False
    assert resource.id is not None and task.resource_id == resource.id  # flush 后 id 已分配
    assert (resource.document_id, resource.course_id, resource.uploader_id) == (100, 10, 1)
    assert resource.chapter_id == 500
    assert task.stage == "precheck"


async def test_create_submission_without_chapter():
    session = FakeSession()
    resource, _, is_resubmit = await create_submission(
        session, _user(1), _doc(), _course(), None, "期末笔记", None
    )
    assert is_resubmit is False and resource.chapter_id is None


async def test_create_submission_duplicate_in_review():
    session = FakeSession()
    existing = _resource(review_status="pending")
    session.seed(existing)
    with pytest.raises(ValueError, match="duplicate"):
        await create_submission(session, _user(1), _doc(), _course(), None, "t", None)
    existing.review_status = "co_reviewing"
    with pytest.raises(ValueError, match="duplicate"):
        await create_submission(session, _user(1), _doc(), _course(), None, "t", None)


async def test_create_submission_approved_conflict():
    session = FakeSession()
    session.seed(_resource(review_status="approved"))
    with pytest.raises(ValueError, match="approved"):
        await create_submission(session, _user(1), _doc(), _course(), None, "t", None)


async def test_create_submission_resubmit_after_reject():
    """已驳回 → 复用原 resource 更新元信息、状态回 pending 重走审核。"""
    session = FakeSession()
    existing = _resource(review_status="rejected", title="旧标题", chapter_id=500)
    session.seed(existing)
    resource, task, is_resubmit = await create_submission(
        session, _user(1), _doc(), _course(), None, "新标题", "新描述"
    )
    assert is_resubmit is True and resource is existing
    assert (resource.title, resource.description, resource.chapter_id) == ("新标题", "新描述", None)
    assert resource.review_status == "pending"
    assert task.stage == "precheck" and task.resource_id == existing.id
    assert task in session.added  # 新建了 precheck 任务，resource 本身复用未重新 add


# ---------------------------------------------------------------------------
# apply_precheck_result：预检结果落地（硬失败驳回 / 推进协审 / 无人兜底）
# ---------------------------------------------------------------------------


async def test_apply_precheck_hard_fail_rejects():
    session = FakeSession()
    resource, task = _resource(), _task()
    session.seed(resource, task)
    result = {"hard_fail": True, "reason": "命中敏感词：包过"}
    await apply_precheck_result(session, task, result)
    assert task.precheck_result == result
    assert resource.review_status == "rejected"
    assert task.stage == "done" and task.finished_at is not None
    (notice,) = _notifications(session, "review_result")
    assert notice.user_id == 1 and notice.body == "命中敏感词：包过"
    assert notice.link == "/library"


async def test_apply_precheck_hard_fail_default_reason():
    session = FakeSession()
    resource, task = _resource(), _task()
    session.seed(resource, task)
    await apply_precheck_result(session, task, {"hard_fail": True})
    (notice,) = _notifications(session, "review_result")
    assert notice.body == "预检未通过"


async def test_apply_precheck_pass_assigns_reviewers(monkeypatch):
    async def fake_assign(session, resource):
        return [5, 6]

    monkeypatch.setattr(workflow, "assign_reviewers", fake_assign)
    session = FakeSession()
    resource, task = _resource(), _task()
    session.seed(resource, task)
    await apply_precheck_result(session, task, {"hard_fail": False})
    assert task.stage == "co_review" and task.assignee_ids == [5, 6]
    assert resource.review_status == "co_reviewing"
    notices = _notifications(session, "review_assign")
    assert [n.user_id for n in notices] == [5, 6]
    assert all(n.link == "/review" and "期末笔记" in n.title for n in notices)


async def test_apply_precheck_pass_no_reviewers_goes_final(monkeypatch):
    """无可用协审员：跳过协审直送终审兜底，任务不卡死。"""

    async def fake_assign(session, resource):
        return []

    monkeypatch.setattr(workflow, "assign_reviewers", fake_assign)
    session = FakeSession()
    resource, task = _resource(), _task()
    session.seed(resource, task)
    await apply_precheck_result(session, task, {"hard_fail": False})
    assert task.stage == "final" and task.assignee_ids == []
    assert resource.review_status == "co_reviewing"
    assert _notifications(session) == []


# ---------------------------------------------------------------------------
# submit_co_verdict：协审提交与多数决
# ---------------------------------------------------------------------------


async def test_co_verdict_wrong_stage():
    session = FakeSession()
    task = _task(stage="final", assignee_ids=[5, 6])
    session.seed(task)
    with pytest.raises(ValueError, match="stage"):
        await submit_co_verdict(session, _user(5), task, "approve", None)


async def test_co_verdict_not_assigned():
    session = FakeSession()
    task = _task(stage="co_review", assignee_ids=[5, 6])
    session.seed(task)
    with pytest.raises(ValueError, match="not_assigned"):
        await submit_co_verdict(session, _user(7), task, "approve", None)


async def test_co_verdict_duplicate_submission():
    session = FakeSession()
    task = _task(stage="co_review", assignee_ids=[5, 6])
    session.seed(task, _record(4001, reviewer_id=5))
    with pytest.raises(ValueError, match="already"):
        await submit_co_verdict(session, _user(5), task, "approve", None)


async def test_co_verdict_reject_requires_comment():
    session = FakeSession()
    task = _task(stage="co_review", assignee_ids=[5, 6])
    session.seed(task)
    for blank in (None, "   "):
        with pytest.raises(ValueError, match="comment_required"):
            await submit_co_verdict(session, _user(5), task, "reject", blank)


async def test_co_verdict_recorded_waiting_for_others():
    session = FakeSession()
    resource, task = _resource(review_status="co_reviewing"), _task(stage="co_review", assignee_ids=[5, 6])
    session.seed(resource, task)
    result = await submit_co_verdict(session, _user(5), task, "approve", None)
    assert result == "recorded"
    assert task.stage == "co_review" and resource.review_status == "co_reviewing"
    (record,) = [o for o in session.added if isinstance(o, ReviewRecord)]
    assert (record.reviewer_id, record.stage, record.verdict) == (5, "co_review", "approve")


async def test_co_verdict_approve_advances_final():
    session = FakeSession()
    resource, task = _resource(review_status="co_reviewing"), _task(stage="co_review", assignee_ids=[5, 6])
    session.seed(resource, task, _record(4001, reviewer_id=5))
    result = await submit_co_verdict(session, _user(6), task, "approve", None)
    assert result == "advanced_final"
    assert task.stage == "final"
    assert resource.review_status == "co_reviewing"  # 上架与否由终审决定


async def test_co_verdict_majority_reject_with_reasons():
    """驳回票过半即驳：理由为各驳回意见用「；」连接，通知投稿人。"""
    session = FakeSession()
    resource, task = _resource(review_status="co_reviewing"), _task(stage="co_review", assignee_ids=[5, 6])
    session.seed(resource, task, _record(4001, reviewer_id=5, verdict="reject", comment="内容错误"))
    result = await submit_co_verdict(session, _user(6), task, "reject", "排版混乱")
    assert result == "rejected"
    assert resource.review_status == "rejected"
    assert task.stage == "done" and task.finished_at is not None
    (notice,) = _notifications(session, "review_result")
    assert notice.user_id == 1 and notice.body == "内容错误；排版混乱"


async def test_co_verdict_single_reject_not_majority():
    """2 人协审 1 驳未过半：第二票 approve 后仍推进终审。"""
    session = FakeSession()
    resource, task = _resource(review_status="co_reviewing"), _task(stage="co_review", assignee_ids=[5, 6])
    session.seed(resource, task, _record(4001, reviewer_id=5, verdict="reject", comment="存疑"))
    result = await submit_co_verdict(session, _user(6), task, "approve", None)
    assert result == "advanced_final" and task.stage == "final"


async def test_co_verdict_departed_reviewer_vote_not_counted():
    """v0.9 已知限制①修正：重指派卸任者的票保留作档案但不计入多数决。

    模拟超时重指派后 assignee_ids 覆盖为 [6, 7]（5 已提交驳回票后卸任）：
    6 驳回时有效票仅 1 张、人数未齐 → recorded；7 通过后有效票 1 驳 1 过、
    驳回未过半 → 推进终审（旧口径会把 5 的卸任驳回票计入，2 驳 1 过误驳）。
    """
    session = FakeSession()
    resource, task = _resource(review_status="co_reviewing"), _task(stage="co_review", assignee_ids=[6, 7])
    departed = _record(4001, reviewer_id=5, verdict="reject", comment="卸任前的驳回")
    session.seed(resource, task, departed)
    result = await submit_co_verdict(session, _user(6), task, "reject", "排版混乱")
    assert result == "recorded"  # 5 的卸任票不计入：有效票 1/2 未齐
    assert task.stage == "co_review" and resource.review_status == "co_reviewing"
    result = await submit_co_verdict(session, _user(7), task, "approve", None)
    assert result == "advanced_final" and task.stage == "final"
    assert departed in session.records  # 卸任者的票仍留档，未被删除


async def test_co_verdict_departed_approve_does_not_block_reject():
    """卸任者的通过票同样不计入：当前 2 名协审员均驳回 → 驳回，
    驳回理由只拼接有效指派人的驳回意见。"""
    session = FakeSession()
    resource, task = _resource(review_status="co_reviewing"), _task(stage="co_review", assignee_ids=[6, 7])
    session.seed(resource, task, _record(4001, reviewer_id=5, verdict="approve"))
    assert await submit_co_verdict(session, _user(6), task, "reject", "内容错误") == "recorded"
    result = await submit_co_verdict(session, _user(7), task, "reject", "排版混乱")
    assert result == "rejected"
    (notice,) = _notifications(session, "review_result")
    assert notice.body == "内容错误；排版混乱"


# ---------------------------------------------------------------------------
# final_verdict：终审（驳回 / 通过派生 public 副本）
# ---------------------------------------------------------------------------


async def test_final_verdict_wrong_stage():
    session = FakeSession()
    task = _task(stage="co_review")
    session.seed(task)
    with pytest.raises(ValueError, match="stage"):
        await final_verdict(session, _user(99, role="admin"), task, "approve", None)


async def test_final_verdict_reject_requires_comment():
    session = FakeSession()
    task = _task(stage="final")
    session.seed(task)
    with pytest.raises(ValueError, match="comment_required"):
        await final_verdict(session, _user(99, role="admin"), task, "reject", "")


async def test_final_verdict_reject():
    session = FakeSession()
    resource, task = _resource(review_status="co_reviewing"), _task(stage="final")
    session.seed(resource, task)
    score, new_doc_id = await final_verdict(
        session, _user(99, role="admin"), task, "reject", "质量不达标"
    )
    assert (score, new_doc_id) == (0, None)
    assert resource.review_status == "rejected"
    assert task.stage == "done" and task.finished_at is not None
    (record,) = [o for o in session.added if isinstance(o, ReviewRecord)]
    assert (record.stage, record.verdict, record.reviewer_id) == ("final", "reject", 99)
    (notice,) = _notifications(session, "review_result")
    assert notice.body == "质量不达标" and notice.link == "/library"
    assert session.documents == []  # 驳回不产生派生文档


async def test_final_verdict_approve_derives_public_copy(monkeypatch):
    """终审通过：派生 public 副本（重编号 chunks + 重 embed + BM25 失效）+ 积分 + 通知。"""
    embedding, store, redis, bm25_calls = _patch_external(monkeypatch)
    monkeypatch.setattr(workflow, "_EMBED_BATCH", 2)  # 缩小批次以覆盖分批 embed
    session = FakeSession()
    uploader, resource, task = _seed_approve_scenario(session)
    score, new_doc_id = await final_verdict(
        session, _user(99, role="admin"), task, "approve", None
    )
    assert score == SCORE_UPLOAD_APPROVED and new_doc_id is not None

    # 状态机终结
    assert resource.review_status == "approved"
    assert task.stage == "done" and task.finished_at is not None

    # 派生文档：复用对象存储原件，scope/status 切换，个人库原件不动
    (new_doc,) = [d for d in session.documents if d.id == new_doc_id]
    src_doc = next(d for d in session.documents if d.id == 100)
    assert src_doc.scope == "personal" and src_doc.status == "parsed"  # 原件不动
    assert new_doc.id == new_doc_id
    assert (new_doc.scope, new_doc.status, new_doc.course_id, new_doc.owner_id) == (
        "public", "parsed", 10, 1,
    )
    assert (new_doc.storage_key, new_doc.md5, new_doc.file_name) == (
        "docs/100.pdf", "md5-src", "笔记100.pdf",
    )

    # chunks 按源 id 排序从 1 重编号 pub_d{id}_{seq:05d}，章节统一为投稿挂载章节
    derived = [c for c in session.chunks if c.document_id == new_doc_id]
    assert [c.chunk_id for c in derived] == [
        f"pub_d{new_doc_id}_{seq:05d}" for seq in (1, 2, 3)
    ]
    assert [c.content for c in derived] == ["内容一", "内容二", "内容三"]
    assert all(c.scope == "public" and c.chapter_id == 500 for c in derived)
    assert all(c.embedding_id == c.chunk_id and c.owner_id == 1 for c in derived)

    # 重 embed（分 2 批）并写入 chunks_public
    assert embedding.batches == [["内容一", "内容二"], ["内容三"]]
    assert store.collections == ["chunks_public"]
    (upsert,) = store.upserts
    assert upsert["collection"] == "coll-chunks_public"
    assert upsert["ids"] == [c.chunk_id for c in derived]
    assert upsert["documents"] == ["内容一", "内容二", "内容三"]
    assert [m["chapter_id"] for m in upsert["metadatas"]] == [500, 500, 500]
    assert upsert["metadatas"][2]["page_num"] == -1  # None 用 -1 哨兵
    assert bm25_calls == [(10, "public")]  # BM25 语料缓存失效

    # 积分结算与实时榜
    assert uploader.score == SCORE_UPLOAD_APPROVED
    (log,) = session.score_logs
    assert (log.user_id, log.delta, log.reason, log.ref_type, log.ref_id) == (
        1, SCORE_UPLOAD_APPROVED, "upload_approved", "resource", 1000,
    )
    assert redis.zsets["rank:major:1"] == {"1": SCORE_UPLOAD_APPROVED}
    assert redis.zsets["rank:course:10"] == {"1": SCORE_UPLOAD_APPROVED}

    # 通知：投稿人上架 + 课程关注者上新（排除投稿人本人与非本课程关注）
    (uploader_notice,) = _notifications(session, "review_result")
    assert uploader_notice.user_id == 1
    assert uploader_notice.title == "投稿已上架：期末笔记"
    assert uploader_notice.link == "/resources/1000"
    (follower_notice,) = _notifications(session, "new_resource")
    assert follower_notice.user_id == 9 and "操作系统" in follower_notice.body


async def test_final_verdict_approve_without_chunks(monkeypatch):
    """原文档无 chunks：跳过 embed/upsert/BM25 失效，仍派生文档并结算积分。"""
    embedding, store, redis, bm25_calls = _patch_external(monkeypatch)
    session = FakeSession()
    uploader, resource, task = _seed_approve_scenario(
        session, with_chunks=False, with_followers=False
    )
    score, new_doc_id = await final_verdict(
        session, _user(99, role="admin"), task, "approve", None
    )
    assert score == SCORE_UPLOAD_APPROVED and new_doc_id is not None
    assert embedding.batches == [] and store.upserts == [] and bm25_calls == []
    assert uploader.score == SCORE_UPLOAD_APPROVED  # 无 chunks 不影响积分与通知
    assert len(_notifications(session, "review_result")) == 1
    assert _notifications(session, "new_resource") == []  # 无关注者


async def test_final_verdict_approve_followers_without_course(monkeypatch):
    """有关注者但课程记录缺失：通知照常发送，课程名按空串降级。"""
    _patch_external(monkeypatch)
    session = FakeSession()
    _, resource, task = _seed_approve_scenario(session, with_course=False)
    await final_verdict(session, _user(99, role="admin"), task, "approve", None)
    (follower_notice,) = _notifications(session, "new_resource")
    assert follower_notice.user_id == 9 and "「」" in follower_notice.body


# ---------------------------------------------------------------------------
# direct_verdict：管理员直审
# ---------------------------------------------------------------------------


async def test_direct_verdict_stage_guards():
    session = FakeSession()
    admin = _user(99, role="admin")
    with pytest.raises(ValueError, match="precheck"):
        await direct_verdict(session, admin, _task(stage="precheck"), "approve", None)
    with pytest.raises(ValueError, match="done"):
        await direct_verdict(session, admin, _task(stage="done"), "approve", None)


async def test_direct_verdict_approve_from_co_review(monkeypatch):
    """协审中直审通过：先推进 final 再复用终审逻辑，行为与终审一致。"""
    _patch_external(monkeypatch)
    session = FakeSession()
    _, resource, task = _seed_approve_scenario(session)
    task.stage = "co_review"
    score, new_doc_id = await direct_verdict(
        session, _user(99, role="admin"), task, "approve", None
    )
    assert score == SCORE_UPLOAD_APPROVED and new_doc_id is not None
    assert task.stage == "done" and resource.review_status == "approved"
    (record,) = [o for o in session.added if isinstance(o, ReviewRecord)]
    assert record.stage == "final"  # 终审记录由 final_verdict 写入，不重复插


async def test_direct_verdict_reject_from_final():
    session = FakeSession()
    resource, task = _resource(review_status="co_reviewing"), _task(stage="final")
    session.seed(resource, task)
    score, new_doc_id = await direct_verdict(
        session, _user(99, role="admin"), task, "reject", "与课程无关"
    )
    assert (score, new_doc_id) == (0, None)
    assert resource.review_status == "rejected" and task.stage == "done"
    (notice,) = _notifications(session, "review_result")
    assert notice.body == "与课程无关"
