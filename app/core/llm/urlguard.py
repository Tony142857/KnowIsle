"""自定义 LLM Key base_url 的 SSRF 防护（v0.9 安全审计）。

用户可配置自有 OpenAI 兼容端点（PUT /api/users/me/llm-key），服务端随后以该
base_url 发起请求；必须限制为公网 HTTPS 地址，防止内网探测与云元数据窃取
（如 169.254.169.254）。校验在配置写入时进行；解析失败一律拒绝。
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


async def _resolve_host(host: str, port: int) -> list:
    """域名解析（单独抽出便于测试 monkeypatch，避免真实 DNS）。"""
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)


async def validate_base_url(base_url: str) -> str | None:
    """校验 base_url 指向公网 HTTPS 端点；合法返回 None，非法返回错误信息。

    规则：必须 https scheme；host 为 IP 字面量时直接判定，为域名时解析并
    要求全部解析结果为公网地址（ipaddress.is_global 已覆盖私网/回环/
    链路本地/保留段/共享地址段 100.64/10 等）。
    """
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        return "base_url 必须使用 https://（防明文窃听与降级）"
    host = parsed.hostname
    if not host:
        return "base_url 缺少主机名"
    try:
        addrs = [ipaddress.ip_address(host)]  # IP 字面量，无需解析
    except ValueError:
        try:
            infos = await _resolve_host(host, parsed.port or 443)
        except OSError:
            return "base_url 域名解析失败"
        addrs = []
        for info in infos:
            try:
                addrs.append(ipaddress.ip_address(info[4][0]))
            except (ValueError, IndexError):
                return "base_url 域名解析结果非法"
        if not addrs:
            return "base_url 域名解析失败"
    for addr in addrs:
        if not addr.is_global:
            return "base_url 不得指向内网/回环/链路本地/保留地址"
    return None
