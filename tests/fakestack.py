"""v0.9 测试加固公共内存替身：tests/test_api_routes.py / test_workers.py / test_pages.py 共用。

与 test_growth.py 同一约定：CI 无 DB/Redis/Chroma/LLM——FakeSession 的 execute
结果按调用顺序出队、get 按 (model, pk) 查表；FakeRedis / FakeObjectStore /
FakeArqPool / FakeEmbedding / FakeVectorStore / FakeLLM 均为内存实现。
"""

from collections import deque
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.identity.rbac import get_current_user_optional
from app.main import app
from app.storage.db import get_db

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)
_ANON = object()  # http_client 的 user 缺省哨兵：匿名（不覆盖认证依赖）


class FakeScalars:
    """模拟 SQLAlchemy ScalarResult：all / first / 直接迭代。"""

    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)

    def first(self):
        return self._values[0] if self._values else None

    def __iter__(self):
        return iter(self._values)


class FakeResult:
    """模拟 SQLAlchemy Result：rows 供 all/first/one/scalars，scalar 供 scalar_one*。"""

    def __init__(self, rows=None, scalar=None):
        self._rows = list(rows or [])
        self._scalar = scalar

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def one(self):
        assert len(self._rows) == 1
        return self._rows[0]

    def scalars(self):
        return FakeScalars(self._rows)

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeSession:
    """最小 AsyncSession 替身：get 查表，execute 按序出队 FakeResult，commit/flush 自动补 id。

    boom=True 时 execute 直接抛异常，用于 SSR 降级分支（DB 不可用容错）。
    """

    def __init__(self, gets=None, results=None, boom=False):
        self._gets = dict(gets or {})
        self._results = deque(results or [])
        self.boom = boom
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self._next_id = 9000

    def add(self, obj):
        self.added.append(obj)

    def add_all(self, objs):
        self.added.extend(list(objs))

    async def get(self, model, pk, **_kwargs):
        return self._gets.get((model, pk))

    async def execute(self, _stmt, *_args, **_kwargs):
        if self.boom:
            raise RuntimeError("DB 不可用")
        assert self._results, "FakeSession.execute 结果队列已空（执行次数超出预置）"
        return self._results.popleft()

    def _assign_ids(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                self._next_id += 1
                obj.id = self._next_id

    async def flush(self):
        self._assign_ids()

    async def commit(self):
        self._assign_ids()
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def session_local(session):
    """生成替换 SessionLocal 的工厂：每次调用返回同一 FakeSession 的异步上下文管理器。"""

    class _CM:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_exc):
            return False

    return _CM


class FakeRedis:
    """最小内存 Redis 替身：kv / 集合 / 计数器 / Sorted Set。"""

    def __init__(self):
        self.kv = {}
        self.sets = {}
        self.zsets = {}
        self.counters = {}
        self.expirations = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def getex(self, key, ex=None):
        return self.kv.get(key)

    async def delete(self, *keys):
        for key in keys:
            self.kv.pop(key, None)

    async def mget(self, keys):
        return [self.kv.get(k) for k in keys]

    async def sadd(self, key, member):
        members = self.sets.setdefault(key, set())
        is_new = member not in members
        members.add(member)
        return 1 if is_new else 0

    async def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key, seconds):
        self.expirations[key] = seconds
        return True

    async def pttl(self, key):
        return 60_000 if key in self.kv else -2

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zincrby(self, key, amount, member):
        zset = self.zsets.setdefault(key, {})
        zset[member] = zset.get(member, 0) + amount
        return zset[member]


class FakeArqPool:
    """ARQ 连接池替身：enqueue_job 记录任务；fail=True 模拟入队失败。"""

    def __init__(self, fail=False):
        self.fail = fail
        self.jobs = []

    async def enqueue_job(self, name, *args):
        if self.fail:
            raise RuntimeError("arq 不可用")
        self.jobs.append((name, *args))


class FakeObjectStore:
    """对象存储替身：key -> bytes 字典；put_object 记录调用。"""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.put_calls = []

    async def get_object(self, key):
        return self.objects[key]

    async def put_object(self, data, key, content_type="application/octet-stream"):
        self.put_calls.append((key, content_type))
        self.objects[key] = data

    async def object_size(self, key):
        data = self.objects.get(key)
        return len(data) if data is not None else None

    async def stream_object(self, key, chunk_size=1024 * 1024):
        yield self.objects[key]


class FakeEmbedding:
    """嵌入模型替身：embed 记录输入并返回固定维度向量。"""

    def __init__(self):
        self.calls = []

    async def embed(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]


class FakeVectorStore:
    """向量库替身：collection 以名字代身，upsert 记录调用。"""

    def __init__(self):
        self.upserts = []

    async def get_or_create_collection(self, name):
        return name

    async def upsert(self, collection, **kwargs):
        self.upserts.append((collection, kwargs))


class FakeLLM:
    """官方模型客户端替身：chat 返回固定文本；fail=True 模拟 LLM 故障。"""

    def __init__(self, text="AI 回答", fail=False):
        self.text = text
        self.fail = fail
        self.prompts = []

    async def chat(self, messages):
        self.prompts.append(messages[0]["content"])
        if self.fail:
            raise RuntimeError("LLM 不可用")
        return self.text, {"tokens": 1}


class GrantRecorder:
    """grant_score 替身：记录全部计分调用。"""

    def __init__(self):
        self.calls = []

    async def __call__(self, session, user_id, delta, reason, ref_type=None, ref_id=None, course_id=None):
        self.calls.append(
            {
                "user_id": user_id,
                "delta": delta,
                "reason": reason,
                "ref_type": ref_type,
                "ref_id": ref_id,
                "course_id": course_id,
            }
        )


def async_value(value):
    """生成异步函数替身：任意参数均返回固定值（替换 get_config / get_arq_pool 等）。"""

    async def _fn(*_args, **_kwargs):
        return value

    return _fn


# ---------------------------------------------------------------------------
# 实体工厂（SimpleNamespace：属性读写自由，足够路由/模板使用）
# ---------------------------------------------------------------------------


def make_user(user_id=1, role="student", **overrides):
    data = {
        "id": user_id, "student_no": f"2023{user_id:04d}", "real_name": "张三",
        "nickname": f"用户{user_id}", "avatar_url": None, "major_id": 3, "grade": "2023级",
        "role": role, "score": 100, "level": 2, "credit": 100, "gpa_public": False,
        "status": "active", "muted_until": None, "created_at": NOW,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_course(course_id=5, scope="public", status="active", owner_id=None, **overrides):
    data = {
        "id": course_id, "major_id": 3, "owner_id": owner_id, "semester_id": None,
        "scope": scope, "name": "操作系统", "description": "课程简介",
        "status": status, "created_at": NOW,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_doc(doc_id=11, owner_id=1, scope="personal", file_type="pdf_textbook",
             status="parsed", **overrides):
    data = {
        "id": doc_id, "course_id": 5, "owner_id": owner_id, "scope": scope,
        "file_name": "笔记.pdf", "file_type": file_type, "storage_key": f"k/{doc_id}",
        "preview_key": None, "md5": f"md5{doc_id}", "status": status, "created_at": NOW,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_chunk(chunk_id="pub_d11_00001", document_id=11, **overrides):
    data = {
        "id": 1, "chunk_id": chunk_id, "document_id": document_id, "course_id": 5,
        "chapter_id": None, "owner_id": 1, "scope": "public", "section": "1.1 概述",
        "page_num": 3, "file_type": "pdf_textbook", "content": "进程与线程的区别",
        "embedding_id": chunk_id,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_resource(resource_id=21, uploader_id=9, review_status="approved", **overrides):
    data = {
        "id": resource_id, "document_id": 11, "course_id": 5, "uploader_id": uploader_id,
        "title": "操作系统笔记", "description": "描述", "chapter_id": None,
        "review_status": review_status, "download_count": 3, "fav_count": 1,
        "rating": None, "rating_count": 0, "download_cost": 0, "created_at": NOW,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_post(post_id=1, author_id=1, board="qa", **overrides):
    data = {
        "id": post_id, "author_id": author_id, "board": board, "major_id": 3,
        "course_id": 5, "chapter_id": None, "title": "测试帖子", "content": "正文内容",
        "tags": ["考研"], "bounty_score": 0, "ai_first_answer": None, "ai_summary": None,
        "accepted_comment_id": None, "view_count": 10, "status": "normal", "created_at": NOW,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_comment(comment_id=31, post_id=1, author_id=9, **overrides):
    data = {
        "id": comment_id, "post_id": post_id, "author_id": author_id, "parent_id": None,
        "content": "评论内容", "is_accepted": False, "created_at": NOW,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_task(task_id=41, resource_id=21, stage="co_review", assignee_ids=None, **overrides):
    data = {
        "id": task_id, "resource_id": resource_id, "stage": stage,
        "precheck_result": {"hard_fail": False}, "assignee_ids": assignee_ids,
        "created_at": NOW, "finished_at": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_major(major_id=3, name="计算机", **overrides):
    data = {
        "id": major_id, "name": name, "code": "CS", "school": None,
        "college": None, "created_at": NOW,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@contextmanager
def http_client(session=None, user=_ANON):
    """挂载 dependency_overrides 的 TestClient：user 缺省匿名，传对象则视为已登录。"""
    overrides = {}
    if session is not None:
        async def _db():
            yield session

        overrides[get_db] = _db
    if user is not _ANON:
        overrides[get_current_user_optional] = lambda: user
    app.dependency_overrides.update(overrides)
    try:
        yield TestClient(app)
    finally:
        for dep in overrides:
            app.dependency_overrides.pop(dep, None)
