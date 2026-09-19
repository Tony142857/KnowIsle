"""知屿 KnowIsle 应用配置（pydantic-settings，从 .env 加载，见文档 §15.3）。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---------- 站点 ----------
    site_name: str = "知屿"
    college_name: str = ""

    # ---------- 学校统一认证 ----------
    auth_provider: str = "email_fallback"  # cas | oidc | email_fallback（开发期默认邮箱降级）
    cas_server_url: str = ""
    auth_callback_url: str = ""

    # ---------- 存储 ----------
    database_url: str = "postgresql+asyncpg://knowisle:knowisle@postgres:5432/knowisle"
    redis_url: str = "redis://redis:6379/0"
    s3_endpoint_url: str = "http://seaweedfs:8333"
    s3_access_key: str = "knowisle"
    s3_secret_key: str = "knowisle_dev_secret"
    s3_bucket: str = "knowisle-files"
    vector_db: str = "chroma"  # chroma | pgvector（演进预留）
    chroma_url: str = "http://chroma:8000"

    # ---------- 官方模型（平台统一提供，默认低价 API） ----------
    official_llm_base_url: str = "https://api.deepseek.com/v1"
    official_llm_api_key: str = ""
    official_model_short: str = "deepseek-chat"
    official_model_medium: str = "deepseek-chat"
    official_model_long: str = "deepseek-chat"

    # ---------- Embedding ----------
    embedding_mode: str = "local"  # local | remote
    embedding_model: str = "bge-small-zh-v1.5"

    # ---------- AI 额度策略 ----------
    ai_daily_free_quota: int = 30
    ai_quota_exchange_rate: int = 10  # 10 贡献分兑换 1 次额外问答

    # ---------- 安全 ----------
    secret_key: str = "change-me"  # 会话签名 + 自定义 Key 加密（Fernet）

    # ---------- 会话与令牌（§7.4 双通道：Redis Session 页面 + JWT API） ----------
    session_cookie_name: str = "knowisle_session"
    session_ttl_seconds: int = 7 * 24 * 3600  # Redis Session 有效期（滑动续期）
    jwt_ttl_seconds: int = 7 * 24 * 3600  # API 通道 JWT 有效期

    # ---------- 邮箱降级认证（§3.1，开发期默认通道） ----------
    email_code_ttl_seconds: int = 600  # 验证码有效期 10 分钟
    email_code_cooldown_seconds: int = 60  # 同学号重发冷却（防滥用，模块 B1）
    allowed_email_suffix: str = ""  # 校园邮箱域名限制，如 ".edu.cn"；空为不限制（演示便利）
    email_code_echo: bool = True  # 开发期假通道：响应回显 dev_code 并写日志（生产务必关闭）


@lru_cache
def get_settings() -> Settings:
    return Settings()
