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
FAIL_KEY_PREFIX = "knowisle:email_fail:"  # 验证码连续失败计数（防爆破，v0.9）
SEND_IP_KEY_PREFIX = "knowisle:email_send_ip:"  # 发码端点 IP 维度限流（v0.9）
MAX_VERIFY_FAILURES = 5  # 同一验证码连续失败上限，达到即作废
SEND_IP_LIMIT = 30  # 单 IP 每小时发码上限
SEND_IP_WINDOW_SECONDS = 3600


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
    await redis.delete(f"{FAIL_KEY_PREFIX}{student_no}")  # 新码生效，清零失败计数

    # 开发期假通道：真实邮件服务接入前，验证码只落日志；
    # 明文仅在 email_code_echo（开发/演示）开启时输出，关闭时只记学号/邮箱
    if settings.email_code_echo:
        logger.info("[email_fallback] 验证码 student_no=%s email=%s code=%s", student_no, email, code)
    else:
        logger.info("[email_fallback] 验证码已生成 student_no=%s email=%s", student_no, email)
    return (code if settings.email_code_echo else None), 0


async def hit_send_ip_limit(redis, ip: str) -> bool:
    """发码端点 IP 维度限流（v0.9）：1 小时上限 30 次，超限返回 True（调用方转 429）。"""
    key = f"{SEND_IP_KEY_PREFIX}{ip}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, SEND_IP_WINDOW_SECONDS)
    return count > SEND_IP_LIMIT


async def verify_code(redis, student_no: str, code: str, *, consume: bool = True) -> bool:
    """校验验证码。consume=True 时验证通过即销毁（一次性使用）；
    consume=False 仅校验不销毁（用于「需补全资料」等中间态，正式登录时才销毁）。

    防爆破（v0.9）：失败计数（INCR + EX，与验证码同 TTL），同学号连续失败
    达到 MAX_VERIFY_FAILURES 即作废当前验证码，需重新发送。
    """
    key = f"{CODE_KEY_PREFIX}{student_no}"
    stored = await redis.get(key)
    if stored is None or stored != code:
        fail_key = f"{FAIL_KEY_PREFIX}{student_no}"
        fails = await redis.incr(fail_key)
        if fails == 1:
            await redis.expire(fail_key, get_settings().email_code_ttl_seconds)
        if fails >= MAX_VERIFY_FAILURES:
            await redis.delete(key)
            await redis.delete(fail_key)
            logger.warning(
                "[email_fallback] 连续验证失败 %d 次，验证码作废 student_no=%s", fails, student_no
            )
        return False
    if consume:
        await redis.delete(key)
    await redis.delete(f"{FAIL_KEY_PREFIX}{student_no}")  # 验证成功清零失败计数
    return True


def check_email_allowed(email: str) -> bool:
    """校园邮箱域名限制（§3.1 edu 域名）；配置为空则不限制（演示便利）。"""
    suffix = get_settings().allowed_email_suffix
    return not suffix or email.lower().endswith(suffix.lower())
