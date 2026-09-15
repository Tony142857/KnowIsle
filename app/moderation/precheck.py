"""自动预检（模块 B2 第一级）：格式可读性 / MD5 查重 / 敏感词 / AI 初评（0-100 分）。

硬失败（格式损坏、重复、命中敏感词）→ 直接驳回并通知，附理由。
"""

# TODO(v0.4): async def precheck(resource_id) -> PrecheckResult
