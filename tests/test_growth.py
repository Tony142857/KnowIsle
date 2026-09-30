"""v0.5 成长体系单元测试（§18.1）：等级阈值 / 下载分成 / grant_score 升级与榜单副作用。

与 test_identity.py 同一约定：CI 无 DB/Redis，session 与 Redis 均为内存替身
（FakeSession / FakeZRedis），不触碰真实存储；榜单 SQL 现算与 settle 对账
链路在容器 compose 环境内验证。
"""

from types import SimpleNamespace

from app.identity import growth
from app.identity.growth import download_share, grant_score, level_for_score
from app.storage.models import Notification, ScoreLog


class FakeZRedis:
    """最小内存 Redis 替身：仅 zadd/zincrby（Sorted Set 用 dict 模拟）。"""

    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zincrby(self, key, amount, member):
        zset = self.zsets.setdefault(key, {})
        zset[member] = zset.get(member, 0) + amount
        return zset[member]


class FakeSession:
    """最小 AsyncSession 替身：add 收集对象，get 固定返回预置用户。"""

    def __init__(self, user):
        self._user = user
        self.added: list = []

    def add(self, obj):
        self.added.append(obj)

    async def get(self, model, pk):
        return self._user


def _user(score: int, level: int = 1, major_id: int | None = 3):
    return SimpleNamespace(id=7, score=score, level=level, major_id=major_id)


# ---------------------------------------------------------------------------
# 等级阈值（纯函数）
# ---------------------------------------------------------------------------


def test_level_for_score_low_tiers():
    assert level_for_score(0) == 1
    assert level_for_score(49) == 1
    assert level_for_score(50) == 2
    assert level_for_score(149) == 2
    assert level_for_score(150) == 3


def test_level_for_score_high_tiers():
    assert level_for_score(399) == 3
    assert level_for_score(400) == 4
    assert level_for_score(999) == 4
    assert level_for_score(1000) == 5
    assert level_for_score(2499) == 5
    assert level_for_score(2500) == 6


def test_level_for_score_out_of_range():
    assert level_for_score(99999) == 6  # 封顶 Lv6
    assert level_for_score(-10) == 1  # 负分按 Lv1


# ---------------------------------------------------------------------------
# 下载分成（纯函数）：上传者 80% 向下取整，其余平台回收
# ---------------------------------------------------------------------------


def test_download_share():
    assert download_share(0) == 0
    assert download_share(1) == 0
    assert download_share(10) == 8
    assert download_share(21) == 16


# ---------------------------------------------------------------------------
# grant_score：明细 + 余额 + 升级通知 + Redis 实时榜
# ---------------------------------------------------------------------------


async def test_grant_score_writes_log_and_balance(monkeypatch):
    monkeypatch.setattr(growth, "get_redis", FakeZRedis)
    user = _user(score=40)
    session = FakeSession(user)
    await grant_score(session, 7, 5, "download_reward", "resource", 11)
    assert user.score == 45
    log = session.added[0]
    assert isinstance(log, ScoreLog)
    assert (log.user_id, log.delta, log.reason, log.ref_type, log.ref_id) == (
        7, 5, "download_reward", "resource", 11,
    )


async def test_grant_score_level_up_notification(monkeypatch):
    monkeypatch.setattr(growth, "get_redis", FakeZRedis)
    user = _user(score=40, level=1)
    session = FakeSession(user)
    await grant_score(session, 7, 20, "upload_approved", "resource", 11, course_id=2)
    assert user.score == 60 and user.level == 2
    notifications = [o for o in session.added if isinstance(o, Notification)]
    assert len(notifications) == 1
    assert notifications[0].type == "level_up"
    assert notifications[0].title == "恭喜升级到 Lv2"
    assert notifications[0].link == "/me"


async def test_grant_score_no_level_up_below_threshold(monkeypatch):
    monkeypatch.setattr(growth, "get_redis", FakeZRedis)
    user = _user(score=40, level=1)
    session = FakeSession(user)
    await grant_score(session, 7, 5, "download_reward")
    assert user.level == 1
    assert not [o for o in session.added if isinstance(o, Notification)]


async def test_grant_score_level_never_drops(monkeypatch):
    """扣分只降分不降级（等级只升不降）。"""
    monkeypatch.setattr(growth, "get_redis", FakeZRedis)
    user = _user(score=60, level=2)
    session = FakeSession(user)
    await grant_score(session, 7, -30, "download_cost", "resource", 11)
    assert user.score == 30 and user.level == 2


async def test_grant_score_updates_realtime_boards(monkeypatch):
    redis = FakeZRedis()
    monkeypatch.setattr(growth, "get_redis", lambda: redis)
    user = _user(score=40, major_id=3)
    session = FakeSession(user)
    await grant_score(session, 7, 20, "upload_approved", "resource", 11, course_id=2)
    assert redis.zsets["rank:major:3"] == {"7": 60}  # 专业榜写新总分
    assert redis.zsets["rank:course:2"] == {"7": 20}  # 课程榜按增量累加
    await grant_score(session, 7, 8, "download_share", "resource", 12, course_id=2)
    assert redis.zsets["rank:course:2"] == {"7": 28}


async def test_grant_score_skips_boards_without_scope(monkeypatch):
    """无 major_id 且未传 course_id 时不写任何榜（扣分调用方不传 course_id）。"""
    redis = FakeZRedis()
    monkeypatch.setattr(growth, "get_redis", lambda: redis)
    user = _user(score=40, major_id=None)
    session = FakeSession(user)
    await grant_score(session, 7, -10, "download_cost", "resource", 11)
    assert redis.zsets == {}


async def test_grant_score_tolerates_redis_failure(monkeypatch):
    """Redis 故障仅 log，不影响计分主流程。"""

    def _broken():
        raise ConnectionError("redis down")

    monkeypatch.setattr(growth, "get_redis", _broken)
    user = _user(score=40)
    session = FakeSession(user)
    await grant_score(session, 7, 20, "upload_approved", "resource", 11, course_id=2)
    assert user.score == 60 and user.level == 2
    assert isinstance(session.added[0], ScoreLog)
