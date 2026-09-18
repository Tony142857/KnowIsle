"""统一认证对接（§3.1）：CAS / OIDC + 邮箱降级通道。

认证层抽象保持不变，切换仅改配置（AUTH_PROVIDER）不改代码。
开发期默认：学号 + 校园邮箱（edu 域名）验证码 + 管理员人工核验
（实现见 identity/email_fallback.py，接口见 api/auth.py）。

CAS/OIDC 为最终对接目标，v0.2 仅预留抽象与路由（§十二接口总表），不对接。
"""

from app.config import get_settings

PROVIDERS = ("cas", "oidc", "email_fallback")


def current_provider() -> str:
    return get_settings().auth_provider


def cas_login_url() -> str:
    """CAS 登录跳转地址（对接后使用）。

    TODO(CAS 对接): 拼接 {CAS_SERVER_URL}/login?service={AUTH_CALLBACK_URL}，
    回调校验 ticket 换取学籍信息（学号/姓名/专业/年级）后按学号建档或登录，
    与 email_fallback 共用「按学号查 user，不存在则建档并绑定 auth_identity」逻辑。
    """
    raise NotImplementedError("CAS/OIDC 对接未启用（v0.2 仅预留抽象，AUTH_PROVIDER=email_fallback）")
