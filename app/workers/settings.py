"""ARQ Worker 入口（§6.1 任务层）。

与 app 同镜像，compose 中以 `arq app.workers.settings.WorkerSettings` 启动。
"""

from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.workers import (
    ai_answer_worker,
    notify_worker,
    parse_worker,
    preview_worker,
    review_timeout_worker,
    review_worker,
    settle_worker,
    summary_worker,
)


class WorkerSettings:
    functions = [
        parse_worker.parse_document,
        review_worker.precheck_submission,
        preview_worker.convert_preview,
        summary_worker.backfill_chapter_summaries,
        notify_worker.send_notification,
        settle_worker.settle_scores,
        review_timeout_worker.review_timeout_scan,
        ai_answer_worker.generate_ai_first_answer,
    ]
    cron_jobs = [
        cron(review_timeout_worker.review_timeout_scan, minute={11, 41}),  # 每 30 分钟扫一次
        cron(settle_worker.settle_scores, hour=3, minute=47),  # 贡献榜每日对账（v0.5）
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 10
    job_timeout = 600  # 解析大文档允许较长超时
    max_tries = 3  # 失败自动重试 3 次后标记 failed（§A1）
