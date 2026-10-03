"""v0.9 安全审计修复单元测试：SECRET_KEY 校验、SSRF base_url 校验、
验证码防爆破 / 发码 IP 限流、ILIKE 通配符转义。

CI 环境无 Postgres/Redis 服务：Redis 走内存替身，域名解析 monkeypatch，
不触碰真实存储与网络（与 test_identity.py 同一约定）。
"""

import socket

import pytest

from app.config import Settings
from app.core.llm import urlguard
from app.identity import email_fallback
from app.identity.email_fallback import MAX_VERIFY_FAILURES

# ---------------------------------------------------------------------------
# SECRET_KEY 弱默认值校验（app/config.py validate_production_secrets）
# ---------------------------------------------------------------------------


def test_weak_secret_key_rejected():
    for weak in ("", "change-me", "changeme", "secret", "knowisle"):
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            Settings(secret_key=weak).validate_production_secrets()


def test_strong_secret_key_accepted():
    Settings(
        secret_key="test-secret-key-0123456789abcdef0123456789abcdef"
    ).validate_production_secrets()


def test_secure_defaults(monkeypatch):
    """v0.9 默认值收紧：echo 关闭、JWT 2 小时、Cookie Secure 默认关（本地 http 演示）。

    与测试进程环境变量 / 挂载 .env 隔离，只验证代码默认值。
    """
    for var in ("EMAIL_CODE_ECHO", "JWT_TTL_SECONDS", "SESSION_COOKIE_SECURE"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None, secret_key="x" * 64)
    assert s.email_code_echo is False
    assert s.jwt_ttl_seconds == 7200
    assert s.session_cookie_secure is False


# ---------------------------------------------------------------------------
# SSRF base_url 校验（app/core/llm/urlguard.py，域名解析 monkeypatch，不发真实 DNS）
# ---------------------------------------------------------------------------


def _addrinfo(*ips: str):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips
    ]


async def test_ssrf_rejects_non_https():
    assert await urlguard.validate_base_url("http://api.deepseek.com/v1") is not None


async def test_ssrf_rejects_ip_literals():
    for url in (
        "https://127.0.0.1/v1",          # 回环
        "https://10.0.0.5/v1",           # 私网
        "https://192.168.1.1/v1",        # 私网
        "https://169.254.169.254/latest/meta-data",  # 云元数据（链路本地）
        "https://0.0.0.0/v1",            # 保留地址
    ):
        assert await urlguard.validate_base_url(url) is not None, url


async def test_ssrf_accepts_public_ip_literal():
    assert await urlguard.validate_base_url("https://8.8.8.8/v1") is None


async def test_ssrf_domain_public(monkeypatch):
    """域名解析结果全部为公网地址 → 放行（演示用例 api.deepseek.com 不误杀）。"""
    async def fake_resolve(host, port):
        return _addrinfo("93.184.216.34")

    monkeypatch.setattr(urlguard, "_resolve_host", fake_resolve)
    assert await urlguard.validate_base_url("https://api.deepseek.com/v1") is None


async def test_ssrf_domain_resolves_private(monkeypatch):
    """域名解析到内网地址 → 拒绝。"""
    async def fake_resolve(host, port):
        return _addrinfo("10.1.2.3")

    monkeypatch.setattr(urlguard, "_resolve_host", fake_resolve)
    assert await urlguard.validate_base_url("https://evil.example.com") is not None


async def test_ssrf_domain_resolves_mixed(monkeypatch):
    """解析结果混入内网地址 → 全部须公网，拒绝。"""
    async def fake_resolve(host, port):
        return _addrinfo("93.184.216.34", "127.0.0.1")

    monkeypatch.setattr(urlguard, "_resolve_host", fake_resolve)
    assert await urlguard.validate_base_url("https://evil.example.com") is not None


async def test_ssrf_domain_resolution_failure(monkeypatch):
    """域名解析失败 → 拒绝。"""
    async def fake_resolve(host, port):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(urlguard, "_resolve_host", fake_resolve)
    error = await urlguard.validate_base_url("https://no-such-host.invalid")
    assert error is not None and "解析失败" in error


# ---------------------------------------------------------------------------
# 验证码防爆破与发码 IP 限流（app/identity/email_fallback.py，FakeRedis）
# ---------------------------------------------------------------------------


class FakeRedis:
    """最小内存 Redis 替身：set/get/delete/expire/ttl/incr（不模拟真实时间流逝）。"""

    def __init__(self):
        self._store: dict[str, tuple[str, int | None]] = {}

    async def set(self, key, value, ex=None):
        self._store[key] = (str(value), ex)

    async def get(self, key):
        item = self._store.get(key)
        return item[0] if item else None

    async def delete(self, key):
        self._store.pop(key, None)

    async def expire(self, key, seconds):
        if key in self._store:
            value, _ = self._store[key]
            self._store[key] = (value, seconds)

    async def incr(self, key):
        value, ex = self._store.get(key, ("0", None))
        value = str(int(value) + 1)
        self._store[key] = (value, ex)
        return int(value)

    async def ttl(self, key):
        if key not in self._store:
            return -2
        return self._store[key][1] or -1


async def test_verify_failures_below_limit_keep_code_valid():
    """连续失败未达上限，正确验证码仍可通过。"""
    redis = FakeRedis()
    dev_code, _ = await email_fallback.send_code(redis, "20230011", "e@stu.edu.cn")
    for _ in range(MAX_VERIFY_FAILURES - 1):
        assert await email_fallback.verify_code(redis, "20230011", "999999") is False
    assert await email_fallback.verify_code(redis, "20230011", dev_code) is True


async def test_verify_failures_at_limit_invalidate_code():
    """连续失败达到上限即作废当前验证码：之后即使输入正确验证码也被拒绝。"""
    redis = FakeRedis()
    dev_code, _ = await email_fallback.send_code(redis, "20230012", "f@stu.edu.cn")
    for _ in range(MAX_VERIFY_FAILURES):
        assert await email_fallback.verify_code(redis, "20230012", "999999") is False
    assert await email_fallback.verify_code(redis, "20230012", dev_code) is False


async def test_resend_resets_failure_counter():
    """重新发送后失败计数清零，新码可正常验证。"""
    redis = FakeRedis()
    await email_fallback.send_code(redis, "20230013", "g@stu.edu.cn")
    for _ in range(MAX_VERIFY_FAILURES):
        await email_fallback.verify_code(redis, "20230013", "999999")
    # 冷却键仍在，直接删键模拟冷却结束后的重发
    await redis.delete("email_code_cd:20230013")
    dev_code, cooldown = await email_fallback.send_code(redis, "20230013", "g@stu.edu.cn")
    assert cooldown == 0 and dev_code is not None
    assert await email_fallback.verify_code(redis, "20230013", "999999") is False  # 第 1 次失败
    assert await email_fallback.verify_code(redis, "20230013", dev_code) is True


async def test_send_ip_rate_limit():
    """单 IP 1 小时 30 次发码上限，超限返回 True（端点转 429）。"""
    redis = FakeRedis()
    for _ in range(email_fallback.SEND_IP_LIMIT):
        assert await email_fallback.hit_send_ip_limit(redis, "203.0.113.10") is False
    assert await email_fallback.hit_send_ip_limit(redis, "203.0.113.10") is True
    # 其他 IP 不受影响
    assert await email_fallback.hit_send_ip_limit(redis, "203.0.113.11") is False


# ---------------------------------------------------------------------------
# ILIKE 通配符转义（app/api/search.py escape_like，admin.py 复用同一函数）
# ---------------------------------------------------------------------------


def test_escape_like():
    from app.api.search import escape_like

    assert escape_like("普通词") == "普通词"
    assert escape_like("100%") == "100\\%"
    assert escape_like("a_b") == "a\\_b"
    assert escape_like("a\\b") == "a\\\\b"
    assert escape_like("%_\\") == "\\%\\_\\\\"
