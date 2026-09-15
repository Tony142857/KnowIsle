"""AI 额度与自定义 Key（§10.1）。

扣减顺序：当日免费额度（ai_quotas 按日建行）→ 兑换余额 bonus_balance；
自定义 Key 用户直接放行（由 user_llm_keys 是否存在记录判定，跨天一致）。
自定义 Key 用 Fernet 对称加密存储（密钥走 SECRET_KEY 环境变量）。
"""

# TODO(v0.5): check_and_consume_quota(user_id) -> None（超限抛 429）/ llm_key CRUD
