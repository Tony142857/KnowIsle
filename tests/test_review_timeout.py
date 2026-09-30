"""v0.5 协审超时重指派 / 管理员改派与直审单元测试（§18.1）。

与 test_moderation.py 同一约定：CI 无 DB/Redis/Chroma/LLM，全部为纯函数与
pydantic 模型校验的内存测试；超时扫描与端点全链路在容器 compose 环境内验证。
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.api.admin import DirectVerdictRequest, ReassignRequest
from app.config import get_settings
from app.moderation.workflow import check_direct_stage
from app.workers.review_timeout_worker import (
    co_review_deadline,
    is_co_overdue,
    merge_assignees,
)

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)


def test_config_default_timeout_hours():
    assert get_settings().review_co_timeout_hours == 48


def test_co_review_deadline():
    assert co_review_deadline(NOW, 48) == NOW - timedelta(hours=48)


def test_is_co_overdue_boundary():
    # created_at 恰好在 48h 前不算超时，超过才算
    assert not is_co_overdue(NOW - timedelta(hours=48), NOW, 48)
    assert is_co_overdue(NOW - timedelta(hours=48, seconds=1), NOW, 48)
    assert not is_co_overdue(NOW - timedelta(hours=1), NOW, 48)


def test_merge_assignees_keep_voted_in_order():
    # 已提交意见者按原指派顺序保留，新指派者追加补足到 needed
    assert merge_assignees([5, 3, 8], [8], [11, 12]) == [8, 11]
    # 已提交意见者已占满 needed 时不再追加
    assert merge_assignees([5, 3, 8], [8, 5], [11, 12]) == [5, 8]


def test_merge_assignees_dedup_and_cap():
    # 新指派者与已提交者/已保留者去重，总量不超过 needed
    assert merge_assignees([5], [5], [5, 11, 12], needed=2) == [5, 11]


def test_merge_assignees_no_voted():
    # 全部超时：旧指派全部替换为新指派者
    assert merge_assignees([5, 8], [], [11, 12]) == [11, 12]
    assert merge_assignees(None, [], [11]) == [11]


def test_merge_assignees_voted_only():
    # 没有新协审员可指派时，仅保留已提交意见者
    assert merge_assignees([5, 8], [8], []) == [8]


def test_check_direct_stage():
    # 协审中/待终审放行；预检未完成与已完结拒绝
    check_direct_stage("co_review")
    check_direct_stage("final")
    with pytest.raises(ValueError, match="precheck"):
        check_direct_stage("precheck")
    with pytest.raises(ValueError, match="done"):
        check_direct_stage("done")


def test_reassign_request_validation():
    assert ReassignRequest(task_id=1).reviewer_ids is None
    assert ReassignRequest(task_id=1, reviewer_ids=[2, 3]).reviewer_ids == [2, 3]
    with pytest.raises(ValidationError):
        ReassignRequest(task_id=1, reviewer_ids=[])


def test_direct_verdict_request_literal():
    assert DirectVerdictRequest(task_id=1, verdict="approve").comment is None
    with pytest.raises(ValidationError):
        DirectVerdictRequest(task_id=1, verdict="pass")


def test_worker_settings_registers_cron():
    # ARQ 注册：超时扫描进入 functions 且配置 cron（每 30 分钟）
    from app.workers.settings import WorkerSettings

    names = [f.__name__ for f in WorkerSettings.functions]
    assert "review_timeout_scan" in names
    assert any(
        job.coroutine.__name__ == "review_timeout_scan" for job in WorkerSettings.cron_jobs
    )
