"""发信。业务代码只依赖下面 Mailer 规定的几个动作，具体怎么发由配置决定。

目前只有开发用的控制台模式：邮件内容打印在后端运行窗口（标准错误输出），
你从窗口里复制验证码填进页面。控制台模式在生产环境会被拒绝启动（见 app_config.py）。
上线前再增加真实发信方式（例如个人邮箱 SMTP），接口不变。

注意：控制台输出里有验证码，只能在你自己的开发电脑上看，不能接入共享日志系统。
"""

import sys
from typing import Protocol

from .app_config import AppConfig
from .errors import ConfigError


class Mailer(Protocol):
    """发信接口：每种邮件一个方法。发送失败时应抛出 MailDeliveryFailed。"""

    def send_registration_code(self, to: str, code: str) -> None: ...

    def send_already_registered(self, to: str) -> None: ...

    def send_password_reset_code(self, to: str, code: str) -> None: ...

    def send_password_changed(self, to: str) -> None: ...


class ConsoleMailer:
    """开发模式：把“邮件”打印到后端窗口。"""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr

    def _print(self, to: str, text: str) -> None:
        print(f"[开发模式邮件] 收件人 {to}：{text}", file=self.stream, flush=True)

    def send_registration_code(self, to: str, code: str) -> None:
        """发注册验证码。"""
        self._print(to, f"注册验证码 {code}，5 分钟内有效")

    def send_already_registered(self, to: str) -> None:
        """提醒邮箱主人：这个邮箱已经有账户（不含验证码）。"""
        self._print(to, "这个邮箱已经注册过账户，请直接登录（未生成验证码）")

    def send_password_reset_code(self, to: str, code: str) -> None:
        """发重置密码验证码。"""
        self._print(
            to, f"重置密码验证码 {code}，5 分钟内有效；如果不是你本人操作，请忽略"
        )

    def send_password_changed(self, to: str) -> None:
        """通知账户邮箱：密码刚被重置。"""
        self._print(
            to,
            "你的密码刚刚被重置，所有设备已退出登录；如果不是你本人操作，请立即重置密码",
        )


def mailer_for(config: AppConfig):
    """按配置创建发信器；以后增加 SMTP 时在这里加一个分支。"""
    if config.mail_mode == "console":
        return ConsoleMailer()
    raise ConfigError(f"不支持的发信方式：{config.mail_mode}")
