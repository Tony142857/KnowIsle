"""v0.9 成长体系补充测试：课程贡献 SQL 现算（compute_course_scores）与
信用裁决（apply_credit_change 限幅 / 通知 / 阶梯自动落库）。

与 test_growth.py 同一约定：FakeSession 内存替身 + monkeypatch 配置读取，
不触碰真实 DB/Redis。
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.identity import growth
from app.identity.growth import apply_credit_change, compute_course_scores
from app.storage.models import CreditLog, Notification


class FakeRowsSession:
    """compute_course_scores 用：execute 按调用序返回预置行集（资源查询、评论查询）。"""

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _query):
        return self._results.pop(0)


# ---------------------------------------------------------------------------
# compute_course_scores：score_logs 现算课程贡献
# ---------------------------------------------------------------------------


async def test_compute_course_scores_aggregates_resources_and_comments():
    """资源类与回答采纳两条查询合并到同一 {course: {user: 总分}}，课程为空跳过。"""
    session = FakeRowsSession(
        [
            [(1, 7, 30), (1, 8, 10)],
            [(1, 7, 5), (None, 9, 3)],
        ]
    )
    assert await compute_course_scores(session) == {1: {7: 35, 8: 10}}


async def test_compute_course_scores_single_course_merges_sources():
    session = FakeRowsSession([[(2, 7, 10)], [(2, 7, 4)]])
    assert await compute_course_scores(session, course_id=2) == {2: {7: 14}}


async def test_compute_course_scores_empty():
    assert await compute_course_scores(FakeRowsSession([[], []])) == {}


# ---------------------------------------------------------------------------
# apply_credit_change：限幅 + CreditLog 留痕 + 通知 + 阶梯处罚落库
# ---------------------------------------------------------------------------


class FakeCreditSession:
    """apply_credit_change 用：add 收集对象，不做真实持久化。"""

    def __init__(self):
        self.added: list = []

    def add(self, obj):
        self.added.append(obj)


def _credit_user(credit: int, status: str = "active", muted_until=None):
    return SimpleNamespace(id=7, credit=credit, status=status, muted_until=muted_until)


def _notifications(session, ntype: str | None = None):
    notes = [o for o in session.added if isinstance(o, Notification)]
    if ntype is not None:
        notes = [n for n in notes if n.type == ntype]
    return notes


async def _fake_mute_days(_session, key):
    assert key == "credit_mute_days"
    return 7


async def test_credit_change_logs_and_returns_clamped():
    """CreditLog 留痕字段完整；返回值即裁决后限幅信用分。"""
    session = FakeCreditSession()
    user = _credit_user(credit=95)
    result = await apply_credit_change(session, user, admin_id=1, delta=-5, reason="灌水")
    assert result == 90 and user.credit == 90
    log = session.added[0]
    assert isinstance(log, CreditLog)
    assert (log.user_id, log.admin_id, log.delta, log.reason) == (7, 1, -5, "灌水")


async def test_credit_change_clamps_upper_bound():
    """恢复上限 100：超出部分截断。"""
    session = FakeCreditSession()
    user = _credit_user(credit=95)
    result = await apply_credit_change(session, user, admin_id=1, delta=20, reason="申诉成立")
    assert result == 100 and user.credit == 100
    notes = _notifications(session, "credit_restore")
    assert len(notes) == 1 and "+20" in notes[0].title


async def test_credit_change_clamps_lower_bound():
    """扣分下限 0：不降至负分；0 分落入冻结档。"""
    session = FakeCreditSession()
    user = _credit_user(credit=10)
    result = await apply_credit_change(session, user, admin_id=1, delta=-50, reason="严重违规")
    assert result == 0 and user.credit == 0
    assert user.status == "frozen"


async def test_credit_change_freeze_tier_locks_account():
    """信用 <40：落库 frozen，清空禁言期限，追加冻结通知（连同裁决 penalty 共两条）。"""
    session = FakeCreditSession()
    user = _credit_user(credit=45, status="muted", muted_until=datetime(2026, 10, 5, tzinfo=UTC))
    await apply_credit_change(session, user, admin_id=1, delta=-10, reason="恶意刷分")
    assert user.status == "frozen" and user.muted_until is None
    penalties = _notifications(session, "penalty")
    assert len(penalties) == 2  # 裁决扣分通知 + 冻结通知
    assert penalties[1].title == "账号已冻结"
    assert "<40" in penalties[1].body


async def test_credit_change_freeze_tier_already_frozen_no_extra_notice():
    """已在冻结态时再次扣分不重复发冻结通知。"""
    session = FakeCreditSession()
    user = _credit_user(credit=35, status="frozen")
    await apply_credit_change(session, user, admin_id=1, delta=-5, reason="继续违规")
    assert user.status == "frozen"
    penalties = _notifications(session, "penalty")
    assert len(penalties) == 1 and penalties[0].title == "信用处罚：-5 分"


async def test_credit_change_mute_tier_sets_deadline(monkeypatch):
    """信用 <60：禁言 credit_mute_days 天，muted_until 取裁决时刻起算。"""
    monkeypatch.setattr(growth, "get_config", _fake_mute_days)
    before = datetime.now(UTC)
    session = FakeCreditSession()
    user = _credit_user(credit=70)
    await apply_credit_change(session, user, admin_id=1, delta=-15, reason="引战")
    assert user.status == "muted"
    assert before + timedelta(days=7) <= user.muted_until <= datetime.now(UTC) + timedelta(days=7)
    penalties = _notifications(session, "penalty")
    assert penalties[-1].title == "账号禁言 7 天"


async def test_credit_change_mute_tier_resets_existing_mute(monkeypatch):
    """已在禁言期再次落入禁言档：期限重置为全新周期。"""
    monkeypatch.setattr(growth, "get_config", _fake_mute_days)
    old_until = datetime.now(UTC) + timedelta(days=1)
    session = FakeCreditSession()
    user = _credit_user(credit=55, status="muted", muted_until=old_until)
    await apply_credit_change(session, user, admin_id=1, delta=-5, reason="再犯")
    assert user.status == "muted"
    assert user.muted_until > datetime.now(UTC) + timedelta(days=6)


async def test_credit_change_restore_unmutes():
    """信用回升至正常档：muted → active，muted_until 清空，追加解除通知。"""
    session = FakeCreditSession()
    user = _credit_user(credit=55, status="muted", muted_until=datetime(2026, 10, 5, tzinfo=UTC))
    result = await apply_credit_change(session, user, admin_id=1, delta=30, reason="申诉成立")
    assert result == 85
    assert user.status == "active" and user.muted_until is None
    restores = _notifications(session, "credit_restore")
    assert len(restores) == 2  # 裁决恢复通知 + 禁言解除通知
    assert restores[1].title == "禁言已解除"


async def test_credit_change_restore_unmutes_in_rate_limit_tier():
    """回升至限流档（60~79）同样解除既有禁言：rate_limited 不落库但须解锁。"""
    session = FakeCreditSession()
    user = _credit_user(credit=50, status="muted", muted_until=datetime(2026, 10, 5, tzinfo=UTC))
    result = await apply_credit_change(session, user, admin_id=1, delta=20, reason="部分撤销")
    assert result == 70  # 仍在 rate_limited 档
    assert user.status == "active" and user.muted_until is None
    assert any(n.title == "禁言已解除" for n in _notifications(session, "credit_restore"))


async def test_credit_change_restore_unfreezes():
    """信用回升至正常档：frozen → active，追加解冻通知。"""
    session = FakeCreditSession()
    user = _credit_user(credit=30, status="frozen")
    await apply_credit_change(session, user, admin_id=1, delta=50, reason="申诉成立")
    assert user.credit == 80
    assert user.status == "active" and user.muted_until is None
    restores = _notifications(session, "credit_restore")
    assert any(n.title == "冻结已解除" for n in restores)


async def test_credit_change_normal_tier_keeps_active():
    """正常信用扣分（仍 ≥80）：不发阶梯通知，状态保持 active。"""
    session = FakeCreditSession()
    user = _credit_user(credit=95)
    await apply_credit_change(session, user, admin_id=1, delta=-5, reason="轻微违规")
    assert user.status == "active"
    assert len(_notifications(session)) == 1  # 仅裁决扣分通知
