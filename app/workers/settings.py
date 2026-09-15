"""ARQ Worker 入口（§6.1 任务层）。

与 app 同镜像，compose 中以 `arq app.workers.settings.WorkerSettings` 启动。
"""

from arq.connections import RedisSettings

from app.config import get_settings
from app.workers import notify_worker, parse_worker, settle_worker


class WorkerSettings:
    functions = [
        parse_worker.parse_document,
        notify_worker.send_notification,
        settle_worker.settle_scores,
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 10
    job_timeout = 600  # 解析大文档允许较长超时
    max_tries = 3  # 失败自动重试 3 次后标记 failed（§A1）
