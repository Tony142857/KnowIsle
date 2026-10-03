"""pytest 全局配置（v0.9）：在首次 get_settings() 之前为测试进程注入安全环境变量。

CI 无 .env，Settings() 走默认值；v0.9 起弱默认 SECRET_KEY 拒绝启动、
EMAIL_CODE_ECHO 默认关闭，这里统一为测试设置安全值（真实环境变量优先）。
"""

import os

os.environ.setdefault(
    "SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef"
)
os.environ.setdefault("EMAIL_CODE_ECHO", "true")  # 既有身份测试依赖开发期 dev_code 回显
