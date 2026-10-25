"""登录回跳地址校验。"""

from urllib.parse import urlsplit


def safe_next_path(value: str | None) -> str:
    """仅允许单斜杠开头的站内路径，拒绝协议相对、外域和反斜杠形式。"""
    if not value:
        return "/"
    if "\\" in value or not value.startswith("/") or value.startswith("//"):
        raise ValueError("unsafe_next")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        raise ValueError("unsafe_next")
    return value
