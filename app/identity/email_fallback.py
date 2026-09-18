"""邮箱降级认证通道（§3.1 开发期默认）：学号 + 校园邮箱验证码 + 管理员核验。

- 验证码：6 位数字，Redis 存 `email_code:{student_no}`，TTL 10 分钟，重发冷却 60 秒。
- 邮件发送为开发期假通道（进度文档 §四「依赖与注意」）：写应用日志 +
  响应回显 dev_code（由 config.email_code_echo 控制），接口契约不变，
  接入真实邮件服务时仅替换 send_code 内部实现。
- 管理员人工核验：验证码通过即建档登录（v0.2 决策，见开发进度文档），
  核验作为运营层面事后抽查，不阻塞登录。
"""

import logging
import secrets

from app.config import get_settings

logger = logging.getLogger("knowisle.auth")

CODE_KEY_PREFIX = "email_code:"
COOLDOWN_KEY_PREFIX = "email_code_cd:"


def generate_code() -> str:
    return f"{secrets.randbelow(1000000):06d}"


async def send_code(redis, student_no: str, email: str) -> tuple[str | None, int]:
    """生成并「发送」验证码。返回 (dev_code, cooldown_seconds)。

    dev_code 仅在 email_code_echo 开启（开发期）时非 None；
    冷却期内重发返回 (None, 剩余冷却秒数)，由调用方转为 429。
    """
    settings = get_settings()
    cooldown_key = f"{COOLDOWN_KEY_PREFIX}{student_no}"
    ttl = await redis.ttl(cooldown_key)
    if ttl > 0:
        return None, ttl

    code = generate_code()
    await redis.set(f"{CODE_KEY_PREFIX}{student_no}", code, ex=settings.email_code_ttl_seconds)
    await redis.set(cooldown_key, "1", ex=settings.email_code_cooldown_seconds)

    # 开发期假通道：真实邮件服务接入前，验证码只落日志
    logger.info("[email_fallback] 验证码 student_no=%s email=%s code=%s", student_no, email, code)
    return (code if settings.email_code_echo else None), 0


async def verify_code(redis, student_no: str, code: str, *, consume: bool = True) -> bool:
    """校验验证码。consume=True 时验证通过即销毁（一次性使用）；
    consume=False 仅校验不销毁（用于「需补全资料」等中间态，正式登录时才销毁）。"""
    key = f"{CODE_KEY_PREFIX}{student_no}"
    stored = await redis.get(key)
    if stored is None or stored != code:
        return False
    if consume:
        await redis.delete(key)
    return True


def check_email_allowed(email: str) -> bool:
    """校园邮箱域名限制（§3.1 edu 域名）；配置为空则不限制（演示便利）。"""
    suffix = get_settings().allowed_email_suffix
    return not suffix or email.lower().endswith(suffix.lower())
