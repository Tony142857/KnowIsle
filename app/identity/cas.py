"""统一认证对接（§3.1）：CAS / OIDC + 邮箱降级通道。

认证层抽象保持不变，切换仅改配置（AUTH_PROVIDER）不改代码。
开发期默认：学号 + 校园邮箱（edu 域名）验证码 + 管理员人工核验。
"""

# TODO(v0.2): cas_login / cas_callback / email_verify；学籍信息仅用于身份核验与专业归属
