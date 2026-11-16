"""网站后端的错误类型。

这些错误的提示文字可以直接显示给操作者看，所以绝不能包含密码、
带密码的连接串，或者数据库服务器返回的原始报错（原始报错里可能有账号名、主机名）。
"""


class WebappError(Exception):
    """所有网站后端错误的基类：提示文字是安全的、可以显示。"""


class ConfigError(WebappError):
    """.env 里缺少必需的配置，或者配置格式不对（例如端口不是数字）。"""


class DatabaseError(WebappError):
    """连不上账户库，或者数据库拒绝了某个操作。"""


class MigrationRefused(DatabaseError):
    """迁移连接不是预期的“迁移账号＋目标数据库”，程序主动拒绝建表。"""


class PasswordRejected(WebappError):
    """新密码不符合规则。

    `code` 是固定的英文编号（如 password_too_short），以后给前端接口用，
    前端按编号显示提示；`message` 是给人看的中文说明。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ── 以下是认证流程的错误（2a）。对外提示刻意保持笼统，不透露账户是否存在、是哪条规则命中。──


class InvalidEmail(WebappError):
    """邮箱格式不符合规则。只说格式问题，不说“这个邮箱有没有注册”。"""

    def __init__(self, message: str = "邮箱格式不正确"):
        super().__init__(message)


class AuthenticationFailed(WebappError):
    """登录失败或当前密码不对。

    邮箱不存在、密码错误、账户被禁用、登录过程中密码刚被改掉，对外全部是这一个错误，
    防止别人借提示判断某个邮箱有没有注册。真实原因只写进审计记录。
    """

    def __init__(self):
        super().__init__("账号或密码错误")


class RateLimited(WebappError):
    """尝试太频繁，需要等一会儿；`retry_after` 是建议等待的秒数（给 Retry-After 响应头用）。"""

    def __init__(self, retry_after: int):
        super().__init__("尝试次数过多，请稍后再试")
        self.retry_after = max(1, int(retry_after))


class VerificationFailed(WebappError):
    """验证码错误、已过期、已用过、输错太多次，或者不是同一个浏览器流程，统一用这个错误。"""

    def __init__(self):
        super().__init__("验证码错误或已失效，请重新获取")


class RegistrationFailed(WebappError):
    """注册凭证无效、已用过、已过期，或该邮箱已有账户：统一提示重新开始注册。"""

    def __init__(self):
        super().__init__("无法完成注册，请重新开始注册流程")


class PasswordResetFailed(WebappError):
    """重置凭证无效、已用过、已过期，或账户已不存在、已被禁用：统一提示重新获取验证码。"""

    def __init__(self):
        super().__init__("无法完成密码重置，请重新获取验证码")


class ServiceBusy(WebappError):
    """服务器暂时忙不过来（例如同时在算的密码哈希太多），请求没有被处理，可以稍后重试。"""

    def __init__(self):
        super().__init__("服务繁忙，请稍后再试")


class MailDeliveryFailed(WebappError):
    """验证邮件没有发出去。验证请求已保存，用户可以稍后重发。"""

    def __init__(self):
        super().__init__("验证邮件发送失败，请稍后重新获取验证码")


# ── 以下是网页接口层的错误（2b）。──


class NotAuthenticated(WebappError):
    """这个操作需要先登录（没有有效的已登录会话）。"""

    def __init__(self):
        super().__init__("请先登录")


class CsrfRejected(WebappError):
    """请求没有通过 CSRF 或来源检查：可能来自别的网站，或者页面已过期需要刷新。"""

    def __init__(self):
        super().__init__("请求校验失败，请刷新页面后重试")
